from __future__ import annotations

from django.conf import settings
from django.db import models


class Reminder(models.Model):
    """一条提醒。锚点固定为截止时间或开始时间，提前量以分钟表示。"""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "待触发"
        SENT = "sent", "已触发"
        CANCELLED = "cancelled", "已取消"

    class Anchor(models.TextChoices):
        DEADLINE = "deadline", "截止时间"
        START_AT = "start_at", "开始时间"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reminders"
    )
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="reminders")
    anchor_kind = models.CharField(max_length=16, choices=Anchor.choices, default=Anchor.DEADLINE)
    offset_minutes = models.PositiveIntegerField(help_text="距锚点提前的分钟数")
    remind_at = models.DateTimeField(db_index=True)
    label = models.CharField(max_length=60, blank=True, default="")
    is_enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["remind_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "anchor_kind", "offset_minutes"], name="uniq_event_reminder_offset"
            )
        ]
        verbose_name = "提醒"
        verbose_name_plural = "提醒"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.event_id} · {self.label or self.offset_minutes}"

    @property
    def active(self) -> bool:
        return self.is_enabled and self.status == self.Status.SCHEDULED


class InAppNotification(models.Model):
    """一条已派发的提醒通知：站内可见，同时邮件推送。

    邮件是主通道（用户不必打开页面就能收到），站内通知是留痕与兜底。
    没有系统级推送，因此提醒在用户打开页面、轮询接口或计划任务跑动时派发；
    过期未派发的提醒会被补偿触发，避免「离线期间静默丢失」（NFR-04）。
    """

    class Level(models.TextChoices):
        INFO = "info", "提醒"
        WARN = "warn", "即将截止"
        OVERDUE = "overdue", "已过期"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    reminder = models.ForeignKey(
        Reminder, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, null=True, blank=True, related_name="notifications"
    )
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=300, blank=True, default="")
    level = models.CharField(max_length=12, choices=Level.choices, default=Level.INFO)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    # 邮件送达时间戳：为空表示还没成功发出去（发送失败会留空，下次派发重试，不会重复轰炸）
    email_sent_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name="邮件已发送于"
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "站内通知"
        verbose_name_plural = "站内通知"

    def __str__(self) -> str:  # pragma: no cover
        return self.title

    @property
    def unread(self) -> bool:
        return self.read_at is None
