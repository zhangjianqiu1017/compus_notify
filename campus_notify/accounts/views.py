from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.forms import (
    PasswordResetConfirmForm,
    PasswordResetRequestForm,
    PreferenceForm,
    RegisterForm,
    SendCodeForm,
)
from accounts.models import EmailVerificationCode, UserPreference
from accounts.services import (
    CodeError,
    code_ttl_minutes,
    dev_show_code,
    find_user_by_email,
    issue_code,
    seconds_until_resend,
)
from core.services import count_user_data, delete_all_user_data
from reminders.services import email_enabled as email_reminder_enabled, recipient_for

SESSION_CODE_KEY = "dev_email_code"
SESSION_EMAIL_KEY = "pending_email"


def _remember_dev_code(request: HttpRequest, email: str, purpose: str, code: str | None) -> None:
    """开发模式：把验证码暂存到会话，好让页面能直接显示（生产环境不会走到这里）。"""
    if code is None or not dev_show_code():
        request.session.pop(SESSION_CODE_KEY, None)
        return
    request.session[SESSION_CODE_KEY] = {"email": email, "purpose": purpose, "code": code}


def _dev_code_context(request: HttpRequest, purpose: str) -> dict:
    payload = request.session.get(SESSION_CODE_KEY)
    if not payload or not dev_show_code():
        return {}
    if payload.get("purpose") != purpose:
        return {}
    return {"dev_code": payload.get("code"), "dev_email": payload.get("email")}


NEUTRAL_RESET_NOTICE = "如果该邮箱已注册，验证码已发送，请查收。"


def _resend_context(request: HttpRequest, purpose: str) -> dict:
    """把「距离可以重发还有多少秒」交给页面，刷新浏览器也绕不过冷却。"""
    email = request.session.get(SESSION_EMAIL_KEY, "")
    if not email:
        return {}
    return {
        "resend_in": seconds_until_resend(email, purpose),
        "resend_email": email,
    }


def _send_code(request: HttpRequest, purpose: str, *, redirect_to: str):
    """公共的发码入口：验证邮箱 → 发码 → 回跳。

    只有「邮箱确实属于一个账号」时才发信：否则系统会变成给任意地址发信的机器，
    可以被用来做邮件轰炸。判断结果不外显（提示语保持一致），避免被探测账号是否存在。
    """
    check_registered = purpose == EmailVerificationCode.Purpose.REGISTER
    form = SendCodeForm(request.POST or None, require_unregistered=check_registered)
    if form.is_valid():
        target = form.cleaned_data["email"]
        user = find_user_by_email(target)
        if user is None and not check_registered:
            messages.info(request, NEUTRAL_RESET_NOTICE)
            return redirect(redirect_to)
        try:
            code = issue_code(target, purpose)
        except CodeError as exc:
            messages.error(request, str(exc))
        else:
            _remember_dev_code(request, target, purpose, code)
            request.session[SESSION_EMAIL_KEY] = target
            if user is None:
                messages.success(
                    request,
                    f"验证码已发送至 {target}，{code_ttl_minutes()} 分钟内有效，请查收邮件。",
                )
            else:
                messages.info(request, NEUTRAL_RESET_NOTICE)
    else:
        for error in form.errors.get("email", []):
            messages.error(request, error)
    return redirect(redirect_to)


# ---------------------------------------------------------------------------
# 注册（邮箱验证码绑定）
# ---------------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def register(request: HttpRequest) -> HttpResponse:
    """FR-01 注册：邮箱收码 → 校验通过才建号 → 直接登录。"""
    if request.user.is_authenticated:
        return redirect("core:home")

    initial = {"email": request.session.get(SESSION_EMAIL_KEY, "")}
    form = RegisterForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        request.session.pop(SESSION_CODE_KEY, None)
        login(request, user, backend="accounts.backends.EmailOrUsernameBackend")
        messages.success(request, "注册成功，欢迎使用校园通知助手。")
        return redirect("core:home")

    return render(
        request,
        "accounts/register.html",
        {
            "form": form,
            **_dev_code_context(request, EmailVerificationCode.Purpose.REGISTER),
            **_resend_context(request, EmailVerificationCode.Purpose.REGISTER),
        },
    )


@require_POST
def send_register_code(request: HttpRequest) -> HttpResponse:
    return _send_code(
        request, EmailVerificationCode.Purpose.REGISTER, redirect_to="accounts:register"
    )


# ---------------------------------------------------------------------------
# 找回密码（邮箱验证码）
# ---------------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def password_reset_request(request: HttpRequest) -> HttpResponse:
    """第一步：输入注册邮箱，发送重置验证码。"""
    if request.user.is_authenticated:
        return redirect("core:home")

    form = PasswordResetRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        user = form.cleaned_data.get("user")
        if user is None:
            # 不存在的邮箱不发信，但提示语与成功时一致，避免被用来探测账号
            messages.info(request, NEUTRAL_RESET_NOTICE)
            return redirect("accounts:password_reset_confirm")
        try:
            code = issue_code(email, EmailVerificationCode.Purpose.RESET)
        except CodeError as exc:
            messages.error(request, str(exc))
        else:
            _remember_dev_code(request, email, EmailVerificationCode.Purpose.RESET, code)
            request.session[SESSION_EMAIL_KEY] = email
            messages.success(request, f"验证码已发送至 {email}，请查收。")
            return redirect("accounts:password_reset_confirm")

    return render(
        request,
        "accounts/password_reset_request.html",
        {
            "form": form,
            **_dev_code_context(request, EmailVerificationCode.Purpose.RESET),
            **_resend_context(request, EmailVerificationCode.Purpose.RESET),
        },
    )


@require_POST
def send_reset_code(request: HttpRequest) -> HttpResponse:
    return _send_code(
        request, EmailVerificationCode.Purpose.RESET, redirect_to="accounts:password_reset"
    )


@require_http_methods(["GET", "POST"])
def password_reset_confirm(request: HttpRequest) -> HttpResponse:
    """第二步：验证码 + 新密码。"""
    if request.user.is_authenticated:
        return redirect("core:home")

    initial = {"email": request.session.get(SESSION_EMAIL_KEY, "")}
    form = PasswordResetConfirmForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save()
        request.session.pop(SESSION_CODE_KEY, None)
        messages.success(request, "密码已重置，请用新密码登录。")
        return redirect("accounts:login")

    remaining = 0
    email = initial.get("email")
    if email:
        remaining = seconds_until_resend(email, EmailVerificationCode.Purpose.RESET)
    return render(
        request,
        "accounts/password_reset_confirm.html",
        {
            "form": form,
            "resend_in": remaining,
            **_dev_code_context(request, EmailVerificationCode.Purpose.RESET),
        },
    )


# ---------------------------------------------------------------------------
# 账号与数据
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def profile(request: HttpRequest) -> HttpResponse:
    preference = UserPreference.for_user(request.user)
    form = PreferenceForm(request.POST or None, instance=preference)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "偏好已保存。")
        return redirect("accounts:profile")
    return render(
        request,
        "accounts/profile.html",
        {
            "form": form,
            "stats": count_user_data(request.user),
            # 邮件提醒的收件地址与总开关状态：让用户先确认「发到哪、发不发」
            "reminder_email": recipient_for(request.user),
            "email_reminder_enabled": email_reminder_enabled(),
        },
    )


@login_required
@require_http_methods(["POST"])
def delete_account(request: HttpRequest) -> HttpResponse:
    """删除账号并清空全部个人数据（NFR-15 / FR-14）。"""
    user = request.user
    confirm = (request.POST.get("confirm") or "").strip()
    if confirm != "删除":
        messages.error(request, "请输入“删除”两个字以确认注销账号。")
        return redirect("accounts:profile")
    delete_all_user_data(user)
    user.delete()
    messages.info(request, "账号与全部数据已删除。")
    return redirect("accounts:login")
