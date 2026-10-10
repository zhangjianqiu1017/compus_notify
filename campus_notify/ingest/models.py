from __future__ import annotations

from django.conf import settings
from django.db import models


class Batch(models.Model):
    """一次导入动作 = 一个批次。

    批次是识别与归并的最小单位：同批消息默认可互相补充，因为用户一次性粘贴的
    内容通常来自同一段对话。
    """

    class Source(models.TextChoices):
        PASTE = "paste", "粘贴文字"
        SHARE = "share", "系统分享"
        MANUAL = "manual", "手动录入"
        OCR = "ocr", "截图识别"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="batches"
    )
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.PASTE)
    note = models.CharField(max_length=200, blank=True, default="")
    stats = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "导入批次"
        verbose_name_plural = "导入批次"

    def __str__(self) -> str:  # pragma: no cover
        return f"批次#{self.pk}({self.get_source_display()})"


class Message(models.Model):
    """一条通知原文。

    raw_text 是识别与证据高亮的唯一来源，除隐私设置要求丢弃外不做改写（NFR-07）。
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="messages"
    )
    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE, related_name="messages", null=True, blank=True
    )
    seq = models.PositiveIntegerField(default=0, help_text="批次内顺序，从 1 开始")
    raw_text = models.TextField()
    sender = models.CharField(max_length=64, blank=True, default="")
    source_label = models.CharField(max_length=64, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True, help_text="消息发送时间，相对时间的锚点")
    message_hash = models.CharField(max_length=64, db_index=True)
    has_sensitive = models.BooleanField(default=False)
    sensitive_kinds = models.CharField(max_length=120, blank=True, default="")
    raw_discarded = models.BooleanField(default=False, help_text="按隐私设置已丢弃原文")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["batch_id", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "message_hash"], name="uniq_batch_message_hash"),
        ]
        verbose_name = "通知原文"
        verbose_name_plural = "通知原文"

    def __str__(self) -> str:  # pragma: no cover
        return f"消息#{self.seq} {self.raw_text[:20]}"

    @property
    def anchor_time(self):
        """相对时间解析的锚点：优先发送时间，其次录入时间。"""
        return self.sent_at or self.created_at

    def display_text(self) -> str:
        return "[原文已按隐私设置丢弃]" if self.raw_discarded else self.raw_text
