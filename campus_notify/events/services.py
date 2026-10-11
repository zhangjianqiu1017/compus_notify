"""事项服务层：落库、字段冲突消解、合并/撤销合并、确认门、待办状态。

这里集中处理「写」的逻辑，视图只做参数解析与渲染。
"""
from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from events.models import Event, EventField, EventMessage
from extraction.fields import (
    DATETIME_FIELDS,
    EVENT_COLUMNS,
    FIELD_DEADLINE,
    FIELD_LABELS,
    FIELD_REQUIREMENT,
    FIELD_TITLE,
    MULTI_VALUE_FIELDS,
    RELATION_CORRECTION,
    RELATION_PRIMARY,
    RELATION_SUPPLEMENT,
)
from extraction.markers import has_cancel_marker
from extraction.resolve import IncomingField, IngestPayload, merge_multi_value, should_replace

# 标题里出现这些词说明抓到的是「补充说明」而不是原始事件名，可以用新标题覆盖
_WEAK_TITLE_MARKERS = ("补充", "更正", "改为", "调整", "说明")


def format_datetime_local(value: datetime | None) -> str:
    if not value:
        return ""
    return timezone.localtime(value).strftime("%Y-%m-%dT%H:%M")


def _create_field(
    event: Event,
    incoming: IncomingField,
    *,
    is_current: bool = True,
    superseded_by: EventField | None = None,
) -> EventField:
    return EventField.objects.create(
        event=event,
        field=incoming.field,
        value_text=incoming.value_text or "",
        value_dt=incoming.value_dt,
        confidence=incoming.confidence,
        source_message=incoming.source_message,
        evidence_span=incoming.evidence_span or "",
        is_current=is_current,
        superseded_by=superseded_by,
        valid_from=incoming.sent_at or timezone.now(),
    )


def _same_value(existing: EventField, incoming: IncomingField) -> bool:
    """判断新识别出的字段值与当前值是否一致（用于避免无意义的历史记录）。"""
    if existing.value_dt or incoming.value_dt:
        return existing.value_dt == incoming.value_dt
    return (existing.value_text or "").strip() == (incoming.value_text or "").strip()


def refresh_denormalized(event: Event, *, touch: bool = True) -> Event:
    """把当前字段值同步到 Event 冗余列（列表/搜索/排序都依赖它）。"""
    currents = {item.field: item for item in event.fields.filter(is_current=True)}
    for field_name, column in EVENT_COLUMNS.items():
        item = currents.get(field_name)
        if column in {"deadline", "start_at", "end_at"}:
            setattr(event, column, item.value_dt if item else None)
        else:
            setattr(event, column, (item.value_text if item else "") or "")
    confidences = [item.confidence for item in currents.values()]
    if confidences:
        event.confidence = round(sum(confidences) / len(confidences), 3)
    if touch:
        event.last_updated_at = timezone.now()
    event.save()
    return event


@transaction.atomic
def apply_incoming_fields(
    event: Event, payload: IngestPayload, *, relation: str, score: float = 0.0
) -> dict:
    """把一次抽取结果写入（或合并进）事件，逐字段做冲突消解。

    返回 {created, replaced, history} 便于确认页提示「哪些字段被更新」。
    """
    created: list[EventField] = []
    replaced: list[tuple[EventField, EventField, str]] = []
    history: list[EventField] = []

    for incoming in payload.fields:
        if incoming.field in MULTI_VALUE_FIELDS:
            existing = event.current_field(incoming.field)
            if existing is None:
                created.append(_create_field(event, incoming))
                continue
            merged_text = merge_multi_value(existing.value_text, incoming.value_text)
            if merged_text != existing.value_text:
                existing.value_text = merged_text
                existing.confidence = max(existing.confidence, incoming.confidence)
                existing.save(update_fields=["value_text", "confidence"])
                history.append(_create_field(event, incoming, is_current=False, superseded_by=existing))
            continue

        existing = event.current_field(incoming.field)
        # 值没变就不写历史：同一条通知被重复导入 / 重新识别时，
        # 否则会把字段历史刷成一堆没有信息量的记录
        if existing is not None and _same_value(existing, incoming):
            continue
        replace, reason = should_replace(existing, incoming)
        if replace:
            new_field = _create_field(event, incoming)
            created.append(new_field)
            if existing is not None:
                existing.is_current = False
                existing.superseded_by = new_field
                existing.change_reason = reason
                existing.save(update_fields=["is_current", "superseded_by", "change_reason"])
                replaced.append((existing, new_field, reason))
        else:
            stale = _create_field(event, incoming, is_current=False, superseded_by=existing)
            stale.change_reason = reason
            stale.save(update_fields=["change_reason"])
            history.append(stale)

    # 标题：原有标题若本身是「补充/更正」类弱标题，允许被更完整的新标题替换
    if payload.title:
        weak = any(marker in (event.title or "") for marker in _WEAK_TITLE_MARKERS) or len(event.title or "") < 4
        if weak and len(payload.title) >= 4:
            event.title = payload.title[:200]

    for message, link_relation in payload.message_links:
        if message is None:
            continue
        # 归并场景下，本条消息在事件中的角色由合并动作决定
        # （provider 在跨消息归并时只能给出 primary）
        effective_relation = link_relation
        if effective_relation == RELATION_PRIMARY and relation and relation != RELATION_PRIMARY:
            effective_relation = relation
        EventMessage.objects.get_or_create(
            event=event,
            message=message,
            defaults={"relation": effective_relation, "score": score},
        )
    if event.primary_message is None and payload.primary_message is not None:
        event.primary_message = payload.primary_message

    if payload.raw_text and has_cancel_marker(payload.raw_text):
        event.is_cancelled = True
        event.review_note = (event.review_note or "")[:200] or "通知中声明取消/作废，请核对。"

    event.save()
    refresh_denormalized(event)
    return {"created": created, "replaced": replaced, "history": history}


@transaction.atomic
def create_event(
    owner,
    payload: IngestPayload,
    *,
    batch=None,
    status: str = Event.Status.DRAFT,
    review_needed: bool = False,
    review_candidate: Event | None = None,
    review_note: str = "",
) -> Event:
    now = timezone.now()
    event = Event.objects.create(
        owner=owner,
        title=(payload.title or "未命名事项")[:200],
        category=payload.category,
        status=status,
        confidence=payload.confidence,
        batch=batch,
        primary_message=payload.primary_message,
        needs_review=review_needed,
        review_candidate=review_candidate,
        review_note=review_note[:300],
        first_seen_at=now,
        last_updated_at=now,
    )
    apply_incoming_fields(event, payload, relation=payload.message_links[0][1] if payload.message_links else "primary", score=0.0)
    return event


@transaction.atomic
def merge_events(source: Event, target: Event, *, relation: str = RELATION_SUPPLEMENT) -> Event:
    """把 source 合并进 target（灰区由用户选择「合并」时调用，或自动合并后撤销用）。"""
    incoming = [
        IncomingField(
            field=item.field,
            value_text=item.value_text,
            value_dt=item.value_dt,
            confidence=item.confidence,
            sent_at=item.valid_from,
            evidence_span=item.evidence_span,
            source_message=item.source_message,
            is_correction=(relation == RELATION_CORRECTION),
        )
        for item in source.fields.filter(is_current=True)
    ]
    payload = IngestPayload(
        title=source.title,
        category=source.category,
        confidence=source.confidence,
        fields=incoming,
        message_links=[],
        raw_text="",
    )
    apply_incoming_fields(target, payload, relation=relation, score=0.0)

    for link in source.links.select_related("message"):
        EventMessage.objects.get_or_create(
            event=target,
            message=link.message,
            defaults={"relation": link.relation, "score": link.score},
        )
    if target.primary_message_id is None:
        target.primary_message = source.primary_message
    if source.is_cancelled:
        target.is_cancelled = True
    target.needs_review = False
    target.review_candidate = None
    target.review_note = ""
    target.save()
    source.delete()
    refresh_denormalized(target)
    return target


@transaction.atomic
def split_message_from_event(event: Event, message) -> Event | None:
    """撤销合并的底层操作：把一条原文从事件中摘出（新事件由调用方创建）。"""
    EventMessage.objects.filter(event=event, message=message).delete()
    event.fields.filter(source_message=message).delete()
    if event.primary_message_id == message.pk:
        remaining = event.links.select_related("message").first()
        event.primary_message = remaining.message if remaining else None
    remaining_links = event.links.count()
    if remaining_links == 0:
        event.delete()
        return None
    event.save()
    refresh_denormalized(event)
    return event


def confirm_event(event: Event, *, reminder_offsets=None) -> Event:
    """确认门通过：写入正式事项并编排提醒。

    保存方式不再由用户选择：确认后事项同时出现在待办与日程里，
    用一个动作换掉一次选择（NFR-08：核心流程更短）。
    """
    from reminders.services import sync_reminders

    event.confirm()
    event.needs_review = False
    event.review_note = ""
    event.review_candidate = None
    event.last_updated_at = timezone.now()
    event.save()
    sync_reminders(event, offsets=reminder_offsets)
    return event


def complete_todo(event: Event) -> Event:
    from reminders.services import sync_reminders

    event.todo_status = Event.TodoStatus.DONE
    event.done_at = timezone.now()
    event.save(update_fields=["todo_status", "done_at", "updated_at"])
    sync_reminders(event)
    return event


def reopen_todo(event: Event) -> Event:
    from reminders.services import sync_reminders

    event.todo_status = Event.TodoStatus.PENDING
    event.done_at = None
    event.save(update_fields=["todo_status", "done_at", "updated_at"])
    sync_reminders(event)
    return event


def record_edit(owner, event: Event | None, field_name: str, old_value: str, new_value: str, source: str) -> None:
    from extraction.models import FieldEditLog

    if (old_value or "") == (new_value or ""):
        return
    FieldEditLog.objects.create(
        owner=owner,
        event=event,
        event_title=(event.title if event else "")[:200],
        field=field_name,
        old_value=str(old_value or "")[:2000],
        new_value=str(new_value or "")[:2000],
        source=source,
    )


@transaction.atomic
def set_fields_from_form(owner, event: Event, values: dict[str, str], *, source: str = "review") -> Event:
    """按表单提交值更新字段（FR-05：人工可修改任意识别结果）。

    values 的键为字段名（含 title/category），值为字符串；空串表示清空该字段。
    """
    from extraction.normalize import resolve_datetime

    title = (values.get(FIELD_TITLE) or "").strip()
    if title and title != event.title:
        record_edit(owner, event, FIELD_TITLE, event.title, title, source)
        event.title = title[:200]

    category = (values.get("category") or "").strip()
    if category and category != event.category:
        record_edit(owner, event, "category", event.category, category, source)
        event.category = category

    anchor = timezone.now()
    editable = list(EVENT_COLUMNS) + [FIELD_REQUIREMENT]
    for field_name in editable:
        if field_name not in values:
            continue
        raw = (values.get(field_name) or "").strip()
        current = event.current_field(field_name)
        old_display = ""
        if current is not None:
            old_display = (
                format_datetime_local(current.value_dt) if field_name in DATETIME_FIELDS else current.value_text
            )

        if not raw:
            if current is not None:
                current.is_current = False
                current.superseded_by = None
                current.change_reason = "用户清空"
                current.save(update_fields=["is_current", "superseded_by", "change_reason"])
                record_edit(owner, event, field_name, old_display, "", source)
            continue

        if field_name in DATETIME_FIELDS:
            parsed = parse_datetime_input(raw)
            if parsed is None:
                continue
            new_display = format_datetime_local(parsed)
            if current is not None and current.value_dt == parsed:
                continue
            if current is not None:
                current.is_current = False
                current.change_reason = "用户修改"
            incoming = IncomingField(
                field=field_name,
                value_text=raw,
                value_dt=parsed,
                confidence=1.0,
                sent_at=anchor,
            )
            new_field = _create_field(event, incoming)
            if current is not None:
                current.superseded_by = new_field
                current.save(update_fields=["is_current", "superseded_by", "change_reason"])
            record_edit(owner, event, field_name, old_display, new_display, source)
        else:
            if current is not None and current.value_text == raw:
                continue
            if current is not None:
                current.is_current = False
                current.change_reason = "用户修改"
            incoming = IncomingField(field=field_name, value_text=raw, confidence=1.0, sent_at=anchor)
            new_field = _create_field(event, incoming)
            if current is not None:
                current.superseded_by = new_field
                current.save(update_fields=["is_current", "superseded_by", "change_reason"])
            record_edit(owner, event, field_name, old_display, raw, source)

    event.save()
    refresh_denormalized(event)
    return event


def parse_datetime_input(raw: str) -> datetime | None:
    """解析 <input type="datetime-local"> 或 ISO 文本。"""
    text = (raw or "").strip().replace(" ", "T")
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return None


# 兼容旧调用名
_parse_form_datetime = parse_datetime_input


def event_field_rows(event: Event) -> list[dict]:
    """给模板用：当前字段 + 证据 + 置信度。"""
    rows = []
    for item in event.fields.filter(is_current=True).select_related("source_message"):
        rows.append(
            {
                "name": item.field,
                "label": FIELD_LABELS.get(item.field, item.field),
                "value": format_datetime_local(item.value_dt) if item.field in DATETIME_FIELDS else item.value_text,
                "display": item.value_text,
                "confidence": item.confidence,
                "evidence": item.evidence_text,
                "message": item.source_message,
            }
        )
    rows.sort(key=lambda row: list(EVENT_COLUMNS).index(row["name"]) if row["name"] in EVENT_COLUMNS else 99)
    return rows
