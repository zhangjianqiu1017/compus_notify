from __future__ import annotations

import calendar as pycalendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from events.forms import (
    ManualEventForm,
    QuickFilterForm,
    ReviewForm,
    EventEditForm,
    default_reminder_selection,
)
from events.models import Event
from events.services import (
    complete_todo,
    confirm_event,
    create_event,
    format_datetime_local,
    merge_events,
    parse_datetime_input,
    reopen_todo,
    set_fields_from_form,
)
from extraction.fields import (
    DATETIME_FIELDS,
    EVENT_COLUMNS,
    FIELD_REQUIREMENT,
    RELATION_SUPPLEMENT,
)
from extraction.pipeline import detach_message
from extraction.resolve import IncomingField, IngestPayload


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
def _initial_from_event(event: Event) -> dict:
    values = {
        "title": event.title,
        "category": event.category,
        "save_as_todo": event.saved_as_todo or bool(event.deadline),
        "save_as_calendar": event.saved_as_calendar or bool(event.start_at or event.end_at),
        "reminder_offsets": [
            str(item.offset_minutes) for item in event.reminders.all()
        ]
        or default_reminder_selection(bool(event.deadline)),
        "is_cancelled": event.is_cancelled,
    }
    for name in EVENT_COLUMNS:
        item = event.current_field(name)
        if item is None:
            values[name] = ""
        elif name in DATETIME_FIELDS:
            values[name] = format_datetime_local(item.value_dt)
        else:
            values[name] = item.value_text
    requirement = event.current_field(FIELD_REQUIREMENT)
    values["requirement"] = requirement.value_text if requirement else ""
    return values


def _user_events(request):
    return Event.objects.for_user(request.user)


# ---------------------------------------------------------------------------
# 确认门（FR-05）
# ---------------------------------------------------------------------------
@login_required
def review(request: HttpRequest, pk: int) -> HttpResponse:
    """保存前的审核页：逐字段核对、看原文证据、处理疑似重复。"""
    event = get_object_or_404(_user_events(request), pk=pk)
    if event.status != Event.Status.DRAFT:
        messages.info(request, "该事项已确认，可直接编辑。")
        return redirect("events:detail", pk=event.pk)

    candidate = event.review_candidate
    # 带上事项已有的提醒点，避免编辑页把用户自定义的提前量清掉
    existing_offsets = list(event.reminders.values_list("offset_minutes", flat=True))
    if request.method == "POST":
        form = ReviewForm(request.POST, prefix=f"e{event.pk}", extra_offsets=existing_offsets)
        if form.is_valid():
            values = form.cleaned_values()
            offsets = form.offsets()
            as_todo = bool(form.cleaned_data.get("save_as_todo"))
            as_calendar = bool(form.cleaned_data.get("save_as_calendar"))

            if form.cleaned_data.get("merge_choice") == "merge" and candidate is not None:
                merge_events(event, candidate, relation=RELATION_SUPPLEMENT)
                confirm_event(
                    candidate, as_todo=as_todo or candidate.saved_as_todo,
                    as_calendar=as_calendar or candidate.saved_as_calendar,
                    reminder_offsets=offsets,
                )
                messages.success(request, f"已合并到《{candidate.title}》，原文已并入该事项。")
                return redirect("events:detail", pk=candidate.pk)

            set_fields_from_form(request.user, event, values, source="review")
            confirm_event(event, as_todo=as_todo, as_calendar=as_calendar, reminder_offsets=offsets)
            messages.success(request, "已保存为正式事项。")
            return redirect("events:detail", pk=event.pk)
    else:
        form = ReviewForm(
            prefix=f"e{event.pk}", initial=_initial_from_event(event), extra_offsets=existing_offsets
        )

    return render(
        request,
        "events/review.html",
        {
            "event": event,
            "form": form,
            "candidate": candidate,
            "links": event.links.select_related("message"),
            "field_rows": _field_rows_with_evidence(event),
        },
    )


def _field_rows_with_evidence(event: Event) -> list[dict]:
    rows = []
    for name in list(EVENT_COLUMNS) + [FIELD_REQUIREMENT]:
        item = event.current_field(name)
        if item is None:
            continue
        rows.append(
            {
                "name": name,
                "field": item,
                "evidence": item.evidence_text,
                "message": item.source_message,
                "confidence": item.confidence,
            }
        )
    return rows


@login_required
def review_list(request: HttpRequest) -> HttpResponse:
    queryset = _user_events(request).filter(status=Event.Status.DRAFT).order_by("-last_updated_at")
    batch_id = request.GET.get("batch")
    if batch_id:
        queryset = queryset.filter(batch_id=batch_id)
    events = list(queryset[:20])
    return render(request, "events/review_list.html", {"events": events, "batch_id": batch_id})


# ---------------------------------------------------------------------------
# 列表 / 待办 / 日程
# ---------------------------------------------------------------------------
@login_required
def event_list(request: HttpRequest) -> HttpResponse:
    form = QuickFilterForm(request.GET or None)
    queryset = _user_events(request).exclude(status=Event.Status.DRAFT)
    if form.is_valid():
        keyword = (form.cleaned_data.get("q") or "").strip()
        category = form.cleaned_data.get("category") or ""
        if keyword:
            queryset = queryset.filter(title__icontains=keyword)
        if category:
            queryset = queryset.filter(category=category)
    queryset = queryset.order_by("-last_updated_at")
    return render(
        request,
        "events/list.html",
        {"events": queryset[:200], "form": form, "draft_count": _user_events(request).filter(status=Event.Status.DRAFT).count()},
    )


@login_required
def todo_list(request: HttpRequest) -> HttpResponse:
    """FR-06 / FR-12：待办视图 + 完成状态。"""
    show = request.GET.get("show", "open")
    category = request.GET.get("category", "")
    queryset = _user_events(request).filter(saved_as_todo=True, status=Event.Status.CONFIRMED)
    if show == "open":
        queryset = queryset.exclude(todo_status=Event.TodoStatus.DONE)
    elif show == "done":
        queryset = queryset.filter(todo_status=Event.TodoStatus.DONE)
    if category:
        queryset = queryset.filter(category=category)
    queryset = queryset.order_by(F("deadline").asc(nulls_last=True), "-last_updated_at")
    done_count = _user_events(request).filter(
        saved_as_todo=True,
        status=Event.Status.CONFIRMED,
        todo_status=Event.TodoStatus.DONE,
    ).count()
    return render(
        request,
        "events/todo.html",
        {
            "events": queryset[:200],
            "show": show,
            "category": category,
            "done_count": done_count,
            "now": timezone.now(),
        },
    )


@login_required
def calendar_view(request: HttpRequest) -> HttpResponse:
    """FR-07：日程视图（与待办共用同一份事件数据）。"""
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if not 1 <= month <= 12:
        month = today.month
    if not 2000 <= year <= 2100:
        year = today.year

    first = date(year, month, 1)
    grid = pycalendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    tz = timezone.get_current_timezone()
    range_start = timezone.make_aware(datetime.combine(grid[0][0], time.min), tz)
    range_end = timezone.make_aware(datetime.combine(grid[-1][-1] + timedelta(days=1), time.min), tz)

    events = (
        _user_events(request)
        .filter(status=Event.Status.CONFIRMED, is_cancelled=False)
        .filter(_deadline_or_start_q(range_start, range_end))
        .prefetch_related("fields")
    )

    by_day: dict[date, list[Event]] = defaultdict(list)
    for event in events:
        anchor = event.start_at or event.deadline
        if anchor is None:
            continue
        by_day[timezone.localtime(anchor).date()].append(event)

    prev_month = first - timedelta(days=1)
    next_month = (first + timedelta(days=31)).replace(day=1)
    rows = [
        [
            {
                "date": day,
                "in_month": day.month == month,
                "is_today": day == today,
                "events": by_day.get(day, []),
            }
            for day in week
        ]
        for week in grid
    ]
    return render(
        request,
        "events/calendar.html",
        {
            "rows": rows,
            "today": today,
            "year": year,
            "month": month,
            "month_label": f"{year} 年 {month} 月",
            "prev": prev_month,
            "next": next_month,
            "weekday_names": ["一", "二", "三", "四", "五", "六", "日"],
        },
    )


def _deadline_or_start_q(range_start, range_end):
    return Q(start_at__gte=range_start, start_at__lt=range_end) | Q(
        deadline__gte=range_start, deadline__lt=range_end
    )


# ---------------------------------------------------------------------------
# 详情 / 编辑 / 删除
# ---------------------------------------------------------------------------
@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(
        _user_events(request).prefetch_related("fields__source_message", "links__message", "reminders"),
        pk=pk,
    )
    history = defaultdict(list)
    for item in event.fields.filter(is_current=False).order_by("-valid_from"):
        history[item.field].append(item)
    return render(
        request,
        "events/detail.html",
        {
            "event": event,
            "field_rows": _field_rows_with_evidence(event),
            "links": event.links.select_related("message"),
            "history": dict(history),
            "reminders": event.reminders.all(),
            "now": timezone.now(),
        },
    )


@login_required
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(_user_events(request), pk=pk)
    anchor_kind = "deadline" if event.deadline else "start_at"
    existing_offsets = list(event.reminders.values_list("offset_minutes", flat=True))
    if request.method == "POST":
        form = EventEditForm(request.POST, prefix=f"e{event.pk}", extra_offsets=existing_offsets)
        if form.is_valid():
            set_fields_from_form(request.user, event, form.cleaned_values(), source="edit")
            event.is_cancelled = bool(form.cleaned_data.get("is_cancelled"))
            event.save(update_fields=["is_cancelled", "updated_at"])
            from reminders.services import sync_reminders

            sync_reminders(event, offsets=form.offsets() or None)
            messages.success(request, "已保存修改，提醒已按新时间重排。")
            return redirect("events:detail", pk=event.pk)
    else:
        form = EventEditForm(
            prefix=f"e{event.pk}", initial=_initial_from_event(event), extra_offsets=existing_offsets
        )
    return render(request, "events/edit.html", {"event": event, "form": form, "anchor_kind": anchor_kind})


@login_required
@require_POST
def delete_event(request: HttpRequest, pk: int) -> HttpResponse:
    """FR-14：删除单条记录（二次确认由前端 confirm 完成）。"""
    event = get_object_or_404(_user_events(request), pk=pk)
    title = event.title
    event.delete()
    messages.success(request, f"已删除《{title}》。")
    return redirect(request.POST.get("next") or "events:list")


@login_required
@require_POST
def bulk_delete(request: HttpRequest) -> HttpResponse:
    """FR-14：批量删除所选事项。

    只删本人数据：`for_user` 限定范围，勾选到别人的 id 也不会生效。
    """
    raw_ids = request.POST.getlist("ids")
    ids = []
    for value in raw_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not ids:
        messages.warning(request, "请先勾选要删除的事项。")
        return redirect(request.POST.get("next") or "events:list")

    queryset = _user_events(request).filter(pk__in=ids)
    titles = list(queryset.values_list("title", flat=True)[:3])
    deleted = queryset.count()
    if not deleted:
        messages.info(request, "这些事项已经不存在了。")
        return redirect(request.POST.get("next") or "events:list")

    queryset.delete()
    sample = "、".join(f"《{title}》" for title in titles)
    suffix = "等" if deleted > len(titles) else ""
    messages.success(request, f"已删除 {deleted} 条事项：{sample}{suffix}。")
    return redirect(request.POST.get("next") or "events:list")


@login_required
@require_POST
def delete_completed(request: HttpRequest) -> HttpResponse:
    """一键清掉已完成的待办。"""
    queryset = _user_events(request).filter(
        saved_as_todo=True, todo_status=Event.TodoStatus.DONE
    )
    count = queryset.count()
    queryset.delete()
    if count:
        messages.success(request, f"已删除 {count} 条已完成的待办。")
    else:
        messages.info(request, "没有已完成的待办。")
    return redirect(request.POST.get("next") or "events:todo")


@login_required
@require_POST
def complete(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(_user_events(request), pk=pk)
    complete_todo(event)
    messages.success(request, f"已完成《{event.title}》。")
    return redirect(request.POST.get("next") or "events:todo")


@login_required
@require_POST
def reopen(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(_user_events(request), pk=pk)
    reopen_todo(event)
    messages.info(request, f"已撤销完成：《{event.title}》。")
    return redirect(request.POST.get("next") or "events:todo")


@login_required
@require_POST
def detach(request: HttpRequest, pk: int, link_id: int) -> HttpResponse:
    """撤销合并：把某条原文从事件中拆出，重新识别为独立事项。"""
    event = get_object_or_404(_user_events(request), pk=pk)
    link = get_object_or_404(event.links.select_related("message"), pk=link_id)
    if event.links.count() <= 1:
        messages.warning(request, "该事项只有一条原文，无需拆分。")
        return redirect("events:detail", pk=event.pk)
    message = link.message
    new_event = detach_message(event, message)
    if new_event is None:
        messages.info(request, "已拆出该原文，未识别到独立事项。")
        return redirect("events:list")
    messages.success(request, "已拆出为新的待确认事项，请核对后再保存。")
    return redirect("events:review", pk=new_event.pk)


# ---------------------------------------------------------------------------
# 手动新建（识别失败的降级路径，5.6）
# ---------------------------------------------------------------------------
@login_required
def create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = ManualEventForm(request.POST, prefix="new")
        if form.is_valid():
            values = form.cleaned_values()
            fields = []
            for name in list(EVENT_COLUMNS) + [FIELD_REQUIREMENT]:
                raw = values.get(name) or ""
                if not raw:
                    continue
                value_dt = None
                if name in DATETIME_FIELDS:
                    value_dt = parse_datetime_input(raw)
                    if value_dt is None:
                        continue
                fields.append(
                    IncomingField(field=name, value_text=raw, value_dt=value_dt, confidence=1.0)
                )
            payload = IngestPayload(
                title=values.get("title") or "未命名事项",
                category=values.get("category") or "other",
                confidence=1.0,
                fields=fields,
                message_links=[],
                raw_text="",
            )
            event = create_event(request.user, payload, status=Event.Status.DRAFT)
            confirm_event(
                event,
                as_todo=bool(form.cleaned_data.get("save_as_todo")),
                as_calendar=bool(form.cleaned_data.get("save_as_calendar")),
                reminder_offsets=form.offsets(),
            )
            messages.success(request, "已新建事项。")
            return redirect("events:detail", pk=event.pk)
    else:
        form = ManualEventForm(prefix="new", initial={"save_as_todo": True})
    return render(request, "events/create.html", {"form": form})
