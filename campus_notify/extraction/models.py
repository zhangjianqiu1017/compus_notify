from __future__ import annotations

from django.conf import settings
from django.db import models


class ExtractionRun(models.Model):
    """一次抽取调用的可观测记录。

    出于数据最小化（NFR-14）只记录元数据与计数，不保存模型原始返回，
    避免在事件表之外再留一份通知原文副本。
    """

    class Status(models.TextChoices):
        OK = "ok", "成功"
        EMPTY = "empty", "无事件"
        ERROR = "error", "失败"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="extraction_runs"
    )
    batch = models.ForeignKey(
        "ingest.Batch", on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
    provider = models.CharField(max_length=32)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OK)
    message_count = models.PositiveIntegerField(default=0)
    event_count = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    warnings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "抽取记录"
        verbose_name_plural = "抽取记录"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.provider}/{self.status} {self.event_count} 个事件"


class FieldEditLog(models.Model):
    """用户对识别结果的修改记录（反馈闭环）。

    这些 diff 是后续优化规则与提示词最有价值的素材，也是 NFR-06「所有识别结果允许编辑」的留痕。
    """

    class Source(models.TextChoices):
        REVIEW = "review", "确认页修改"
        EDIT = "edit", "事项编辑"
        REMOVE = "remove", "清空字段"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="field_edits"
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="field_edits"
    )
    event_title = models.CharField(max_length=200, blank=True, default="")
    field = models.CharField(max_length=32)
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.REVIEW)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "字段修改记录"
        verbose_name_plural = "字段修改记录"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.field}: {self.old_value} → {self.new_value}"
