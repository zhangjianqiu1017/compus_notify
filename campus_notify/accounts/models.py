from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class UserPreference(models.Model):
    """用户隐私与提醒偏好。

    store_raw_messages 对应 NFR-15：用户可以决定是否保存通知原文。
    关闭后系统仍会完成识别，但导入结束后立即丢弃原文，只保留结构化字段。
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preference"
    )
    store_raw_messages = models.BooleanField(
        default=True, verbose_name="保存通知原文", help_text="关闭后识别完成即丢弃原文，只保留结构化事项。"
    )
    warn_sensitive = models.BooleanField(
        default=True, verbose_name="敏感信息提示", help_text="原文含学号/手机号等时给出提示。"
    )
    default_reminder_enabled = models.BooleanField(default=True, verbose_name="默认创建提醒")
    reminder_email_enabled = models.BooleanField(
        default=True,
        verbose_name="提醒发到邮箱",
        help_text="关闭后提醒仍会出现在站内「提醒」页，只是不再发邮件。",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "用户偏好"
        verbose_name_plural = "用户偏好"

    def __str__(self) -> str:  # pragma: no cover - 便于 admin 展示
        return f"{self.user} 的偏好"

    @classmethod
    def for_user(cls, user) -> "UserPreference":
        if not user.is_authenticated:
            raise ValueError("未登录用户没有偏好设置")
        preference, _ = cls.objects.get_or_create(user=user)
        return preference


class EmailVerificationCode(models.Model):
    """邮箱验证码：注册绑定邮箱与验证码登录共用。

    只存验证码哈希（不落明文），并带过期时间、错误次数与使用标记，
    避免验证码被读库复用，或被无限次暴力尝试。
    """

    class Purpose(models.TextChoices):
        REGISTER = "register", "注册绑定邮箱"
        LOGIN = "login", "验证码登录"

    email = models.EmailField(db_index=True)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "purpose", "created_at"])]
        verbose_name = "邮箱验证码"
        verbose_name_plural = "邮箱验证码"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.email} / {self.get_purpose_display()} / {self.created_at:%m-%d %H:%M}"

    @property
    def expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def usable(self) -> bool:
        return self.used_at is None and not self.expired

    def mark_used(self) -> None:
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])
