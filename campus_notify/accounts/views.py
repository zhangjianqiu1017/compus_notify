from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.forms import (
    LoginCodeForm,
    PreferenceForm,
    RegisterForm,
    SendCodeForm,
)
from accounts.models import EmailVerificationCode, UserPreference
from accounts.services import (
    CodeError,
    code_ttl_minutes,
    dev_show_code,
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

    只给「该发码的邮箱」发信，否则系统会变成给任意地址发信的机器，可被用来邮件轰炸：
    * 注册用途：邮箱**没注册过**才发（`require_unregistered`）；
    * 登录用途：邮箱**已注册**才发（`require_registered`）。

    校验放在表单层（`SendCodeForm`），好处是「该不该发」和「发不发得出去」只有一处判断。
    顺序也不能反——先发信再校验的话，被拒的邮箱照样会收到一封邮件。
    """
    form = SendCodeForm(
        request.POST or None,
        require_unregistered=purpose == EmailVerificationCode.Purpose.REGISTER,
        require_registered=purpose == EmailVerificationCode.Purpose.LOGIN,
    )
    if form.is_valid():
        target = form.cleaned_data["email"]
        try:
            code = issue_code(target, purpose)
        except CodeError as exc:
            messages.error(request, str(exc))
        else:
            _remember_dev_code(request, target, purpose, code)
            request.session[SESSION_EMAIL_KEY] = target
            messages.success(
                request,
                f"验证码已发送至 {target}，{code_ttl_minutes()} 分钟内有效，请查收邮件。",
            )
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
# 验证码登录（邮箱 + 验证码，不需要密码）
# ---------------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def code_login(request: HttpRequest) -> HttpResponse:
    """FR-01 验证码登录：填邮箱 + 验证码，验证码正确即登录。

    和密码登录（accounts:login）是两条并行的入口，互不影响。
    """
    if request.user.is_authenticated:
        return redirect("core:home")

    initial = {"email": request.session.get(SESSION_EMAIL_KEY, "")}
    form = LoginCodeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        request.session.pop(SESSION_CODE_KEY, None)
        login(request, user, backend="accounts.backends.EmailCodeLoginBackend")
        messages.success(request, "登录成功，欢迎回来。")
        return redirect("core:home")

    return render(
        request,
        "accounts/login_code.html",
        {
            "form": form,
            **_dev_code_context(request, EmailVerificationCode.Purpose.LOGIN),
            **_resend_context(request, EmailVerificationCode.Purpose.LOGIN),
        },
    )


@require_POST
def send_login_code(request: HttpRequest) -> HttpResponse:
    return _send_code(
        request, EmailVerificationCode.Purpose.LOGIN, redirect_to="accounts:code_login"
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
