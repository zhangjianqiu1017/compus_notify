"""搜索、导出、删除等跨应用服务。"""
from __future__ import annotations

from datetime import timedelta
from datetime import timezone as dt_timezone

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from events.models import Event, EventMessage
from extraction.models import ExtractionRun, FieldEditLog
from ingest.models import Batch, Message
from reminders.models import InAppNotification, Reminder


def count_user_data(user) -> dict:
    return {
        "events": Event.objects.for_user(user).count(),
        "drafts": Event.objects.for_user(user).filter(status=Event.Status.DRAFT).count(),
        "todos": Event.objects.for_user(user).filter(saved_as_todo=True).count(),
        # 首页角标用的「还没做完的事」：确认后的事项都算待办，所以这里要按完成状态收口
        "open_todos": Event.objects.for_user(user)
        .filter(
            status=Event.Status.CONFIRMED,
            is_cancelled=False,
            saved_as_todo=True,
            todo_status=Event.TodoStatus.PENDING,
        )
        .count(),
        "messages": Message.objects.filter(owner=user).count(),
        "reminders": Reminder.objects.filter(owner=user).count(),
        "notifications": InAppNotification.objects.filter(owner=user).count(),
        "edits": FieldEditLog.objects.filter(owner=user).count(),
    }


def search(user, query: str, *, limit: int = 60) -> dict:
    """FR-11：按标题、正文、课程、地点检索历史通知与事项。"""
    keyword = (query or "").strip()
    if not keyword:
        return {"keyword": "", "events": [], "messages": [], "total": 0}

    events = (
        Event.objects.for_user(user)
        .filter(
            Q(title__icontains=keyword)
            | Q(course__icontains=keyword)
            | Q(location__icontains=keyword)
            | Q(organizer__icontains=keyword)
            | Q(fields__value_text__icontains=keyword, fields__is_current=True)
        )
        .distinct()
        .order_by("-last_updated_at")[:limit]
    )
    messages = (
        Message.objects.filter(owner=user, raw_discarded=False, raw_text__icontains=keyword)
        .order_by("-created_at")[:limit]
    )
    return {
        "keyword": keyword,
        "events": list(events),
        "messages": list(messages),
        "total": len(events) + len(messages),
    }


TIMELINE_LABELS = {"deadline": "截止", "start_at": "开始"}


def _nearest_anchor(event, now, soon):
    """事项在未来窗口内最近的那个时间点。

    一个事项可能既有截止时间又有开始时间：首页只挑最近的那个作为排序依据，
    并告诉界面这是「截止」还是「开始」，这样一条事项不会在两个位置各出现一次。
    """
    options = []
    if event.deadline and now <= event.deadline <= soon:
        options.append(("deadline", event.deadline))
    if event.start_at and now <= event.start_at <= soon:
        options.append(("start_at", event.start_at))
    if not options:
        return None, None
    # 时间相同时按「截止」展示：那才是需要动手的那件事
    return min(options, key=lambda item: (item[1], item[0] != "deadline"))


def upcoming(user, *, days: int = 7, limit: int = 20) -> dict:
    """首页数据：已过截止时间的事项 + 未来 N 天的时间轴。

    确认后的事项既是待办也是日程（见 `Event.confirm`），所以首页不再拆成
    「截止 / 日程」两栏——一条事项只出现一次，按它最近的时间点排序。
    """
    now = timezone.now()
    soon = now + timedelta(days=days)
    active = (
        Event.objects.for_user(user)
        .filter(status=Event.Status.CONFIRMED, is_cancelled=False)
        .exclude(todo_status=Event.TodoStatus.DONE)
    )
    overdue = list(
        active.filter(todo_status=Event.TodoStatus.PENDING, deadline__lt=now)
        .order_by("deadline")[:limit]
    )
    # 已经出现在「已过截止时间」里的，时间轴上不再重复
    window = active.filter(
        Q(deadline__gte=now, deadline__lte=soon) | Q(start_at__gte=now, start_at__lte=soon)
    ).exclude(pk__in=[item.pk for item in overdue])

    rows = []
    for event in window:
        kind, anchor = _nearest_anchor(event, now, soon)
        if anchor is None:
            continue
        rows.append(
            {"event": event, "kind": kind, "kind_label": TIMELINE_LABELS[kind], "anchor": anchor}
        )
    rows.sort(key=lambda row: (row["anchor"], row["kind"] != "deadline"))
    return {"timeline": rows[:limit], "overdue": overdue, "days": days}


def _ics_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def _ics_fold(line: str) -> str:
    """RFC 5545：每行不超过 75 字节，超出部分折行。"""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks = []
    while encoded:
        size = 75 if not chunks else 74
        cut = encoded[:size]
        while True:
            try:
                piece = cut.decode("utf-8")
                break
            except UnicodeDecodeError:
                cut = cut[:-1]
        chunks.append(piece)
        encoded = encoded[len(cut):]
    return "\r\n ".join(chunks)


def events_to_ics(events) -> str:
    """US-12：导出日程（.ics 可被系统日历导入）。"""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//CampusNotify//校园通知智能管理//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    stamp = timezone.now().astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for event in events:
        start = event.start_at or event.deadline
        if start is None:
            continue
        end = event.end_at
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:event-{event.pk}@campus-notify")
        lines.append(f"DTSTAMP:{stamp}")
        lines.append(f"DTSTART:{start.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        if end:
            lines.append(f"DTEND:{end.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        lines.append(_ics_fold(f"SUMMARY:{_ics_escape(event.title)}"))
        if event.location:
            lines.append(_ics_fold(f"LOCATION:{_ics_escape(event.location)}"))
        description_bits = []
        if event.course:
            description_bits.append(f"课程：{event.course}")
        if event.deadline:
            description_bits.append(
                "截止：" + timezone.localtime(event.deadline).strftime("%Y-%m-%d %H:%M")
            )
        if event.link:
            description_bits.append(f"链接：{event.link}")
        if event.is_cancelled:
            description_bits.append("状态：已取消")
        if description_bits:
            lines.append(_ics_fold("DESCRIPTION:" + _ics_escape("；".join(description_bits))))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def export_payload(user) -> dict:
    """FR-14：导出个人数据（JSON）。"""
    events = Event.objects.for_user(user).prefetch_related("fields", "links__message", "reminders")
    payload = {
        "exported_at": timezone.now().isoformat(),
        "account": {"username": user.username, "email": user.email},
        "events": [],
        "messages": [],
        "reminders": [],
    }
    for event in events:
        payload["events"].append(
            {
                "id": event.pk,
                "title": event.title,
                "category": event.get_category_display(),
                "status": event.get_status_display(),
                "saved_as_todo": event.saved_as_todo,
                "saved_as_calendar": event.saved_as_calendar,
                "todo_status": event.get_todo_status_display(),
                "is_cancelled": event.is_cancelled,
                "deadline": event.deadline.isoformat() if event.deadline else None,
                "start_at": event.start_at.isoformat() if event.start_at else None,
                "end_at": event.end_at.isoformat() if event.end_at else None,
                "location": event.location,
                "course": event.course,
                "link": event.link,
                "confidence": event.confidence,
                "fields": [
                    {
                        "field": item.field,
                        "value": item.value_text,
                        "value_dt": item.value_dt.isoformat() if item.value_dt else None,
                        "confidence": item.confidence,
                        "is_current": item.is_current,
                        "change_reason": item.change_reason,
                        "valid_from": item.valid_from.isoformat(),
                    }
                    for item in event.fields.all()
                ],
                "sources": [
                    {
                        "message_seq": link.message.seq,
                        "relation": link.relation,
                        "sender": link.message.sender,
                        "sent_at": link.message.sent_at.isoformat() if link.message.sent_at else None,
                        "raw_text": link.message.display_text(),
                    }
                    for link in event.links.all()
                ],
                "reminders": [
                    {
                        "offset_minutes": item.offset_minutes,
                        "remind_at": item.remind_at.isoformat(),
                        "status": item.get_status_display(),
                        "is_enabled": item.is_enabled,
                    }
                    for item in event.reminders.all()
                ],
            }
        )
    for message in Message.objects.filter(owner=user):
        payload["messages"].append(
            {
                "id": message.pk,
                "seq": message.seq,
                "sender": message.sender,
                "sent_at": message.sent_at.isoformat() if message.sent_at else None,
                "raw_text": message.display_text(),
            }
        )
    for reminder in Reminder.objects.filter(owner=user).select_related("event"):
        payload["reminders"].append(
            {
                "event": reminder.event.title,
                "remind_at": reminder.remind_at.isoformat(),
                "status": reminder.get_status_display(),
            }
        )
    return payload


@transaction.atomic
def delete_all_user_data(user) -> dict:
    """FR-14 / NFR-15：清空全部个人数据（保留账号本身）。"""
    counts = count_user_data(user)
    InAppNotification.objects.filter(owner=user).delete()
    Reminder.objects.filter(owner=user).delete()
    EventMessage.objects.filter(event__owner=user).delete()
    Event.objects.filter(owner=user).delete()
    FieldEditLog.objects.filter(owner=user).delete()
    ExtractionRun.objects.filter(owner=user).delete()
    Message.objects.filter(owner=user).delete()
    Batch.objects.filter(owner=user).delete()
    return counts


def purge_raw_messages(user) -> int:
    """只删除原文，保留结构化事项。"""
    from ingest.services import purge_raw_texts

    return purge_raw_texts(user)
