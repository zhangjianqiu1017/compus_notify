from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from extraction.fields import (
    CATEGORY_CHOICES,
    CATEGORY_OTHER,
    FIELD_CHOICES,
    RELATION_CHOICES,
    RELATION_LABELS,
    RELATION_PRIMARY,
)


class EventQuerySet(models.QuerySet):
    def for_user(self, user):
        """NFR-12：任何查询都必须以所有者为界。"""
        if not user.is_authenticated:
            return self.none()
        return self.filter(owner=user)

    def order_by_time(self):
        """US-10：按时间排序（截止优先，没有截止才看开始）。

        有截止时间的事项（作业、报名）按截止时间排——那才是必须动手的时刻，
        此时开始时间不参与排序；没有截止时间的事项（考试、讲座、比赛）按开始
        时间排，否则它们会因为没有截止时间而全部沉到列表底部。
        两个时间都没填的排最后。
        """
        return self.annotate(time_anchor=Coalesce("deadline", "start_at")).order_by(
            F("time_anchor").asc(nulls_last=True), "-last_updated_at", "-id"
        )


class Event(models.Model):
    """一个校园事项（事件聚合根）。"""

    class Status(models.TextChoices):
        DRAFT = "draft", "待确认"
        CONFIRMED = "confirmed", "已确认"
        ARCHIVED = "archived", "已归档"

    class TodoStatus(models.TextChoices):
        NONE = "none", "未作为待办"
        PENDING = "pending", "待完成"
        DONE = "done", "已完成"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="events"
    )
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)

    # 由 EventField 当前值同步下来的冗余列，用于列表/搜索/排序
    course = models.CharField(max_length=120, blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    organizer = models.CharField(max_length=120, blank=True, default="")
    link = models.URLField(max_length=500, blank=True, default="")
    contact = models.CharField(max_length=120, blank=True, default="")
    deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    start_at = models.DateTimeField(null=True, blank=True, db_index=True)
    end_at = models.DateTimeField(null=True, blank=True)

    # FR-06 / FR-07：事项一经确认即同时进入待办与日程，用户不必选择
    saved_as_todo = models.BooleanField(default=False)
    saved_as_calendar = models.BooleanField(default=False)
    todo_status = models.CharField(
        max_length=12, choices=TodoStatus.choices, default=TodoStatus.NONE, db_index=True
    )
    done_at = models.DateTimeField(null=True, blank=True)

    is_cancelled = models.BooleanField(default=False, help_text="通知中声明取消/作废")

    confidence = models.FloatField(default=0.0)
    needs_review = models.BooleanField(default=False)
    review_note = models.CharField(max_length=300, blank=True, default="")
    review_candidate = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="review_of"
    )

    batch = models.ForeignKey(
        "ingest.Batch", on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    primary_message = models.ForeignKey(
        "ingest.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="primary_of"
    )

    first_seen_at = models.DateTimeField(default=timezone.now)
    last_updated_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-last_updated_at", "-id"]
        indexes = [
            models.Index(fields=["owner", "status", "todo_status"]),
            models.Index(fields=["owner", "deadline"]),
        ]
        verbose_name = "事项"
        verbose_name_plural = "事项"

    def __str__(self) -> str:  # pragma: no cover
        return self.title

    # ------------------------------------------------------------------
    @property
    def is_confirmed(self) -> bool:
        return self.status == self.Status.CONFIRMED

    @property
    def is_open_todo(self) -> bool:
        return self.saved_as_todo and self.todo_status == self.TodoStatus.PENDING and not self.is_cancelled

    @property
    def primary_time(self):
        """列表与日历上的主时间：截止优先，其次开始时间。"""
        return self.deadline or self.start_at

    @property
    def overdue(self) -> bool:
        anchor = self.primary_time
        if not anchor or self.todo_status == self.TodoStatus.DONE:
            return False
        return anchor < timezone.now()

    @property
    def overdue_kind(self) -> str:
        """哪一个时间点让这条事项过期了：deadline / start_at / 空串（没过期）。

        列表里只把这个时间点标红，避免只看截止时间而漏掉活动开始时间。
        """
        if not self.overdue:
            return ""
        return "deadline" if self.deadline else "start_at"

    @property
    def is_ended(self) -> bool:
        """结束时间已经过去。没填结束时间时退化为看开始时间。"""
        anchor = self.end_at or self.start_at
        return anchor is not None and anchor < timezone.now()

    @property
    def is_closed(self) -> bool:
        """事项是否已经完成/结束：列表里整条弱化成灰色，一眼看出不必再处理。

        覆盖四种收尾方式：待办已完成、通知已取消、事项已归档、时间已过。
        注意「已过期」的待办（还没做完但截止时间过了）不算结束，仍要红色提醒。
        """
        return (
            self.todo_status == self.TodoStatus.DONE
            or self.is_cancelled
            or self.status == self.Status.ARCHIVED
            or self.is_ended
        )

    def source_key(self) -> str:
        """来源指纹：用于判断是否同一渠道发布。"""
        links = list(self.links.all()[:1])
        if not links:
            return ""
        message = links[0].message
        sender = (message.sender or "").strip()
        label = (message.source_label or "").strip()
        return "|".join(part for part in (label, sender) if part)

    def current_field(self, name: str):
        for item in self.fields.all():
            if item.field == name and item.is_current:
                return item
        return None

    def field_value(self, name: str) -> str:
        item = self.current_field(name)
        if item is None:
            return ""
        if item.value_dt:
            return timezone.localtime(item.value_dt).strftime("%Y-%m-%d %H:%M")
        return item.value_text

    def relation_labels(self) -> list[str]:
        return [RELATION_LABELS.get(link.relation, link.relation) for link in self.links.all()]

    def confirm(self) -> None:
        """确认门通过。

        确认即同时进入待办与日程两个视图（同一份数据的两种看法），
        用户不需要在确认时勾选保存方式；已完成的事项保持完成状态。
        """
        self.status = self.Status.CONFIRMED
        self.saved_as_todo = True
        self.saved_as_calendar = True
        if self.todo_status != self.TodoStatus.DONE:
            self.todo_status = self.TodoStatus.PENDING


class EventField(models.Model):
    """字段级值 + 溯源。is_current=False 的记录是历史版本，被 superseded_by 指向的后继替代。"""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="fields")
    field = models.CharField(max_length=32, choices=FIELD_CHOICES)
    value_text = models.TextField(blank=True, default="")
    value_dt = models.DateTimeField(null=True, blank=True)
    confidence = models.FloatField(default=0.6)
    source_message = models.ForeignKey(
        "ingest.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="contributed_fields"
    )
    evidence_span = models.CharField(max_length=40, blank=True, default="", help_text="原文中的字符区间 start:end")
    is_current = models.BooleanField(default=True, db_index=True)
    superseded_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="supersedes"
    )
    change_reason = models.CharField(max_length=120, blank=True, default="")
    valid_from = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["field", "-is_current", "-valid_from"]
        indexes = [models.Index(fields=["event", "field", "is_current"])]
        verbose_name = "字段值"
        verbose_name_plural = "字段值"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.get_field_display()}={self.value_dt or self.value_text}"

    @property
    def span(self) -> tuple[int, int] | None:
        raw = (self.evidence_span or "").strip()
        if ":" not in raw:
            return None
        start, _, end = raw.partition(":")
        try:
            return int(start), int(end)
        except ValueError:
            return None

    @span.setter
    def span(self, value: tuple[int, int] | None) -> None:
        self.evidence_span = f"{value[0]}:{value[1]}" if value else ""

    @property
    def evidence_text(self) -> str:
        if not self.source_message or self.source_message.raw_discarded:
            return ""
        bounds = self.span
        if not bounds:
            return ""
        return self.source_message.raw_text[bounds[0]: bounds[1]]


class EventMessage(models.Model):
    """事件与原文的多对多关系，relation 说明这条消息在事件里的角色。"""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="links")
    message = models.ForeignKey("ingest.Message", on_delete=models.CASCADE, related_name="event_links")
    relation = models.CharField(max_length=16, choices=RELATION_CHOICES, default=RELATION_PRIMARY)
    score = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["message__seq", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "message"], name="uniq_event_message"),
        ]
        verbose_name = "事件原文关联"
        verbose_name_plural = "事件原文关联"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.event_id}<-{self.message_id}({self.relation})"
