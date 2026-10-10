"""邮箱验证码服务：发码、校验、防刷。

安全要点（这些不是为了好看，缺一个都能被绕过）：
* 只存哈希，不落明文；
* 有效期 + 单码错误次数上限 + 重发冷却；
* 同一邮箱同一用途只保留一条有效码，重新发码即作废旧码；
* 验证成功后立即标记已用，防止一码多用。
"""
from __future__ import annotations

import logging
import secrets
import smtplib

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from accounts.models import EmailVerificationCode

logger = logging.getLogger(__name__)
CODE_LENGTH = 6


class CodeError(Exception):
    """验证码不可用（由调用方转成表单错误展示）。"""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def code_ttl_minutes() -> int:
    return int(getattr(settings, "EMAIL_CODE_TTL_MINUTES", 10))


def max_attempts() -> int:
    return int(getattr(settings, "EMAIL_CODE_MAX_ATTEMPTS", 5))


def resend_seconds() -> int:
    return int(getattr(settings, "EMAIL_CODE_RESEND_SECONDS", 60))


def dev_show_code() -> bool:
    """开发便利：把验证码直接显示在页面上。

    必须同时满足 DEBUG=True 与显式开启，生产环境（DEBUG=False）永远关闭。
    """
    return bool(getattr(settings, "DEBUG", False)) and bool(
        getattr(settings, "EMAIL_DEV_SHOW_CODE", False)
    )


def email_taken(email: str) -> bool:
    normalized = normalize_email(email)
    return User.objects.filter(email__iexact=normalized).exists() or User.objects.filter(
        username__iexact=normalized
    ).exists()


def find_user_by_email(email: str):
    normalized = normalize_email(email)
    user = User.objects.filter(email__iexact=normalized).order_by("id").first()
    if user is not None:
        return user
    return User.objects.filter(username__iexact=normalized).order_by("id").first()


def latest_code(email: str, purpose: str) -> EmailVerificationCode | None:
    return (
        EmailVerificationCode.objects.filter(email=normalize_email(email), purpose=purpose)
        .order_by("-created_at")
        .first()
    )


def seconds_until_resend(email: str, purpose: str) -> int:
    """距离可以再次发码还有多少秒（0 表示可以发）。"""
    record = latest_code(email, purpose)
    if record is None:
        return 0
    passed = (timezone.now() - record.created_at).total_seconds()
    remaining = int(resend_seconds() - passed)
    return max(0, remaining)


def send_code_email(email: str, code: str, purpose: str) -> None:
    purpose_text = "注册绑定邮箱" if purpose == EmailVerificationCode.Purpose.REGISTER else "重置密码"
    subject = f"【校园通知助手】{purpose_text}验证码"
    body = (
        f"你的验证码是：{code}\n\n"
        f"用途：{purpose_text}\n"
        f"有效期：{code_ttl_minutes()} 分钟\n"
        "如果这不是你本人的操作，请忽略本邮件，你的账号不会受到影响。\n"
    )
    send_mail(
        subject,
        body,
        getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
        [email],
        fail_silently=False,
    )


def _friendly_mail_error(exc: Exception) -> str:
    """把 SMTP 的原始报错翻译成用户能照做的提示。

    邮箱拼错时 QQ/多数服务商回 550（收件人不存在），这种要说"检查地址"，
    而不是让用户茫然地"稍后重试"。
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "该邮箱地址可能不存在，请检查后重试。"
    if isinstance(exc, smtplib.SMTPDataError) and getattr(exc, "smtp_code", None) == 550:
        return "该邮箱地址可能不存在，请检查后重试。"
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError)):
        return "邮件服务暂时不可用，请稍后重试。"
    return "验证码邮件发送失败，请稍后重试。"


@transaction.atomic
def issue_code(email: str, purpose: str) -> str:
    """生成并发送验证码，返回明文（仅用于开发模式下回显给页面）。

    发信失败时整体回滚：不留无效验证码，也不会因为冷却时间把用户挡在门外
    （transaction.atomic 在 CodeError 抛出时回滚本次的建码与作废旧码）。
    """
    normalized = normalize_email(email)
    remaining = seconds_until_resend(normalized, purpose)
    if remaining > 0:
        raise CodeError(f"发送太频繁，请 {remaining} 秒后再试。")

    # 旧码一律作废，避免同时存在多个有效验证码
    EmailVerificationCode.objects.filter(
        email=normalized, purpose=purpose, used_at__isnull=True
    ).update(used_at=timezone.now())

    code = "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))
    EmailVerificationCode.objects.create(
        email=normalized,
        purpose=purpose,
        code_hash=make_password(code),
        expires_at=timezone.now() + timezone.timedelta(minutes=code_ttl_minutes()),
    )
    try:
        send_code_email(normalized, code, purpose)
    except Exception as exc:  # SMTP 超时 / 认证失败 / 收件人被拒 / 网络不通
        logger.exception("验证码邮件发送失败：%s", normalized)
        raise CodeError(_friendly_mail_error(exc)) from exc
    return code


def verify_code(email: str, purpose: str, code: str) -> None:
    """校验验证码；不通过时抛 CodeError，通过时不消费（消费由调用方决定时机）。"""
    normalized = normalize_email(email)
    code = (code or "").strip().replace(" ", "")
    if not code:
        raise CodeError("请输入邮箱验证码。")

    record = latest_code(normalized, purpose)
    if record is None:
        raise CodeError("请先获取验证码。")
    if record.used_at is not None:
        raise CodeError("该验证码已使用，请重新获取。")
    if record.expired:
        raise CodeError("验证码已过期，请重新获取。")
    if record.attempts >= max_attempts():
        raise CodeError("错误次数过多，请重新获取验证码。")

    if not check_password(code, record.code_hash):
        record.attempts += 1
        record.save(update_fields=["attempts"])
        left = max(0, max_attempts() - record.attempts)
        if left:
            raise CodeError(f"验证码不正确，还可以尝试 {left} 次。")
        raise CodeError("错误次数过多，请重新获取验证码。")

    # 命中但不消费：调用方在真正完成注册 / 改密后再 mark_used，
    # 避免「码对了但密码不合法」时把码浪费掉。
    return None


def consume_code(email: str, purpose: str) -> None:
    record = latest_code(email, purpose)
    if record is not None and record.used_at is None:
        record.mark_used()


def purge_expired_codes(days: int = 7) -> int:
    """清理过期验证码记录（可挂计划任务）。"""
    cutoff = timezone.now() - timezone.timedelta(days=days)
    deleted, _detail = EmailVerificationCode.objects.filter(created_at__lt=cutoff).delete()
    return deleted
