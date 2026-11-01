from __future__ import annotations

import calendar as pycalendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
    FIELD_LABELS,
    FIELD_REQUIREMENT,
    RELATION_SUPPLEMENT,
)
from extraction.pipeline import detach_message
from extraction.resolve import IncomingField, IngestPayload
from reminders.services import OFFSET_UNIT_CHOICES, dispatch_due, offset_parts


WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]
# 手机端日历格子里最多排几个彩点：再多也看不清，反正点开当天页就有全部
CELL_DOT_LIMIT = 3


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
def _initial_from_event(event: Event) -> dict:
    values = {
        "title": event.title,
        "category": event.category,
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


def _after_confirm(request: HttpRequest, pk: int) -> HttpResponse:
    """确认通过后回哪里。

    还有草稿排在「待确认」里就回那个队列继续处理（一次导入往往有多条），
    全部处理完了才去看这条的结果——比每条都跳一次详情页少绕好几圈。
    """
    remaining = (
        _user_events(request).filter(status=Event.Status.DRAFT).exclude(pk=pk).count()
    )
    if remaining:
        messages.info(request, f"还有 {remaining} 条识别结果待确认。")
        return redirect(f"{reverse('events:todo')}?show=draft")
    return redirect("events:detail", pk=pk)


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

            if form.cleaned_data.get("merge_choice") == "merge" and candidate is not None:
                merge_events(event, candidate, relation=RELATION_SUPPLEMENT)
                confirm_event(candidate, reminder_offsets=offsets)
                messages.success(request, f"已合并到《{candidate.title}》，原文已并入该事项。")
                return _after_confirm(request, candidate.pk)

            set_fields_from_form(request.user, event, values, source="review")
            confirm_event(event, reminder_offsets=offsets)
            messages.success(request, "已保存，待办和日程里都能看到这条事项。")
            return _after_confirm(request, event.pk)
    else:
        form = ReviewForm(
            prefix=f"e{event.pk}", initial=_initial_from_event(event), extra_offsets=existing_offsets
        )

    original_blocks, attention = _original_evidence(event)
    return render(
        request,
        "events/review.html",
        {
            "event": event,
            "form": form,
            "candidate": candidate,
            "original_blocks": original_blocks,
            "attention": attention,
        },
    )


def _original_evidence(event: Event) -> tuple[list[dict], list[dict]]:
    """相关原文的就地证据标注 + 需要人工留意的字段，review / detail 共用。

    返回 (blocks, attention)：
    * blocks：每条相关原文，附上落在它身上的字段证据区间——证据直接标进原文，
      「哪个值来自哪一句」一眼可见，不必再把整段原文按字段重复摆一遍；
    * attention：未能在原文中定位到证据、或置信度偏低的字段。这是原「识别依据」栏
      承担的信息，去掉那一栏后必须另找出口，否则歧义会被静默放过（NFR-05）。
    """
    annotations: dict[int, list[dict]] = defaultdict(list)
    attention: list[dict] = []
    for name in list(EVENT_COLUMNS) + [FIELD_REQUIREMENT]:
        item = event.current_field(name)
        if item is None:
            continue
        label = FIELD_LABELS.get(name, name)
        bounds = item.span
        message = item.source_message
        if bounds and bounds[1] > bounds[0] and message is not None and not message.raw_discarded:
            annotations[message.pk].append(
                {"start": bounds[0], "end": bounds[1], "label": label}
            )
            if item.confidence < 0.6:
                attention.append(
                    {"name": label, "reason": f"置信度偏低 {round(item.confidence * 100)}%"}
                )
        else:
            attention.append({"name": label, "reason": "未在原文中定位到证据"})

    blocks = [
        {"link": link, "annotations": annotations.get(link.message_id, [])}
        for link in event.links.select_related("message")
    ]
    return blocks, attention


def _field_rows(event: Event) -> list[dict]:
    """字段当前值 + 置信度 + 来源，供详情页表格。"""
    rows = []
    for name in list(EVENT_COLUMNS) + [FIELD_REQUIREMENT]:
        item = event.current_field(name)
        if item is None:
            continue
        rows.append(
            {
                "name": name,
                "field": item,
                "message": item.source_message,
                "confidence": item.confidence,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 待办 / 待确认 / 日程
# ---------------------------------------------------------------------------
@login_required
def todo_list(request: HttpRequest) -> HttpResponse:
    """FR-06 / FR-12 / US-10：待办视图 + 完成状态。

    「待确认」是待办下的一个子栏目（`?show=draft`）：识别完但用户还没确认的草稿
    全在这里排队，确认通过后才落进下面的待办表。原来独立的「事项」栏目已经并入
    本页——两者本来就是同一份数据（`Event.confirm` 一次确认同时进待办与日程），
    分两个平级入口只会让用户猜该点哪个。随之搬过来的还有 US-10 的分类筛选
    与 FR-14 的批量删除，功能一样不少，只是不再各有一页。
    """
    show = request.GET.get("show", "open")
    if show not in {"draft", "open", "done", "all"}:
        show = "open"

    form = QuickFilterForm(request.GET or None)
    keyword = category = ""
    if form.is_valid():
        keyword = (form.cleaned_data.get("q") or "").strip()
        category = form.cleaned_data.get("category") or ""

    drafts = _user_events(request).filter(status=Event.Status.DRAFT)
    # 角标按「全部待确认」算，不受下面的筛选影响：筛选是为了看，不是为了藏
    draft_count = drafts.count()
    batch_id = (request.GET.get("batch") or "").strip()

    if show == "draft":
        # 刚识别完的一批草稿：带上 batch 就只看这一批，跟着导入的落点走
        if batch_id.isdigit():
            drafts = drafts.filter(batch_id=int(batch_id))
        if keyword:
            drafts = drafts.filter(title__icontains=keyword)
        if category:
            drafts = drafts.filter(category=category)
        events = list(drafts.order_by("-last_updated_at")[:50])
    else:
        queryset = _user_events(request).filter(
            saved_as_todo=True, status=Event.Status.CONFIRMED
        )
        if show == "open":
            queryset = queryset.exclude(todo_status=Event.TodoStatus.DONE)
        elif show == "done":
            queryset = queryset.filter(todo_status=Event.TodoStatus.DONE)
        if keyword:
            queryset = queryset.filter(title__icontains=keyword)
        if category:
            queryset = queryset.filter(category=category)
        # 排序仍用同一把时间尺子（events/models.py::EventQuerySet.order_by_time）：
        # 有截止时间的按截止排，只有开始时间的活动（讲座、比赛）按开始时间排，
        # 否则它们会因为没有截止时间而全部沉到列表底部。
        events = list(queryset.order_by_time()[:200])

    confirmed_todos = _user_events(request).filter(
        saved_as_todo=True, status=Event.Status.CONFIRMED
    )
    done_count = confirmed_todos.filter(todo_status=Event.TodoStatus.DONE).count()
    return render(
        request,
        "events/todo.html",
        {
            "events": events,
            "show": show,
            "form": form,
            "keyword": keyword,
            "category": category,
            "draft_count": draft_count,
            "done_count": done_count,
            "open_count": confirmed_todos.count() - done_count,
            "total_count": confirmed_todos.count(),
            "batch_id": batch_id,
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
        [_calendar_cell(day, month, today, by_day.get(day, [])) for day in week]
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
            "weekday_names": WEEKDAY_NAMES,
        },
    )


def _calendar_cell(day: date, month: int, today: date, events: list[Event]) -> dict:
    """一个格子要渲染的全部信息。

    桌面版格子里放得下几条日程文字，窄屏放不下（10px 字、40px 宽，既看不清也
    点不准），所以格子同时准备了两份表达：`events` 给桌面版列表，`dots` 给窄屏
    的彩点，`day_url` 是窄屏整格可点之后要去的「当天安排」页。
    """
    return {
        "date": day,
        "in_month": day.month == month,
        "is_today": day == today,
        "events": events,
        "dots": [_dot_class(event) for event in events[:CELL_DOT_LIMIT]],
        "day_url": reverse("events:day", args=[day.year, day.month, day.day]),
        "label": f"{day.month} 月 {day.day} 日",
    }


def _dot_class(event: Event) -> str:
    """彩点颜色与桌面版条目保持一致：橙=截止、蓝=日程、灰=已完成/已结束。"""
    kind = "is-event" if event.start_at else "is-deadline"
    return f"{kind} is-closed" if event.is_closed else kind


def _deadline_or_start_q(range_start, range_end):
    return Q(start_at__gte=range_start, start_at__lt=range_end) | Q(
        deadline__gte=range_start, deadline__lt=range_end
    )


def _anchor_q(range_start, range_end):
    """按「主时间」落在哪一天筛选，与月历把事项画进哪一格是同一把尺子。

    月历用的是 `事件.start_at or 事件.deadline`（见 `_calendar_cell` 的调用方），
    所以这里也必须是「有开始时间只看开始时间」。若写成 `Q(start_at in ...) |
    Q(deadline in ...)`，一条 10/1 开始、10/20 截止的报名会同时出现在两天的
    当天页里，而月历上只画在 10/1——点开格子看到的东西和格子上的对不上。
    """
    return Q(start_at__gte=range_start, start_at__lt=range_end) | Q(
        start_at__isnull=True, deadline__gte=range_start, deadline__lt=range_end
    )


@login_required
def day_view(request: HttpRequest, year: int, month: int, day: int) -> HttpResponse:
    """FR-07：某一天的全部安排——窄屏从日程格子点进来的一页。

    月历在手机上每格只有 40px 宽，格子里那几条 10px 的日程文字看不清也点不准。
    与其把字再压小，不如让格子只回答「这天有没有事」（彩点），点开这一页看当天
    的完整清单：标题、时间、状态、完成按钮都在这里。
    """
    try:
        target = date(year, month, day)
    except ValueError:
        raise Http404("日期不存在")

    tz = timezone.get_current_timezone()
    range_start = timezone.make_aware(datetime.combine(target, time.min), tz)
    range_end = range_start + timedelta(days=1)
    events = list(
        _user_events(request)
        .filter(status=Event.Status.CONFIRMED, is_cancelled=False)
        .filter(_anchor_q(range_start, range_end))
        .prefetch_related("fields")
    )
    # 按当天的钟点排：9:00 的讲座排在 23:59 的作业截止前面
    events.sort(key=lambda event: event.start_at or event.deadline)

    previous_day = target - timedelta(days=1)
    following_day = target + timedelta(days=1)
    return render(
        request,
        "events/day.html",
        {
            "day": target,
            "events": events,
            "open_count": sum(1 for event in events if not event.is_closed),
            "is_today": target == timezone.localdate(),
            "weekday_name": WEEKDAY_NAMES[target.weekday()],
            "prev_url": reverse(
                "events:day", args=[previous_day.year, previous_day.month, previous_day.day]
            ),
            "next_url": reverse(
                "events:day", args=[following_day.year, following_day.month, following_day.day]
            ),
            "calendar_year": target.year,
            "calendar_month": target.month,
            "now": timezone.now(),
        },
    )


# ---------------------------------------------------------------------------
# 详情 / 编辑 / 删除
# ---------------------------------------------------------------------------
@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    # FR-08/NFR-04：提醒管理就在这一页（不再有独立的提醒页），
    # 打开即可见每条提醒的真实状态；顺带派发到点提醒（离线恢复的补偿也走这里）。
    # 必须先派发再读提醒，否则页面上会停留在派发前的旧状态。
    fired = dispatch_due(user=request.user, request=request)
    event = get_object_or_404(
        _user_events(request).prefetch_related("fields__source_message", "links__message", "reminders"),
        pk=pk,
    )
    history = defaultdict(list)
    for item in event.fields.filter(is_current=False).order_by("-valid_from"):
        history[item.field].append(item)
    original_blocks, _attention = _original_evidence(event)
    # prefetch 是在派发之前进的缓存，这里刷新一下才能显示刚送达的状态
    reminders = []
    for reminder in event.reminders.all():
        reminder.refresh_from_db()
        # 每条提醒都带上「数量 + 单位」，页面据此渲染成可编辑的输入框
        reminder.amount, reminder.unit = offset_parts(reminder.offset_minutes)
        reminders.append(reminder)
    return render(
        request,
        "events/detail.html",
        {
            "event": event,
            "field_rows": _field_rows(event),
            "original_blocks": original_blocks,
            "history": dict(history),
            "reminders": reminders,
            "reminder_units": OFFSET_UNIT_CHOICES,
            "reminder_fired": fired,
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
    return redirect(request.POST.get("next") or "events:todo")


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
        return redirect(request.POST.get("next") or "events:todo")

    queryset = _user_events(request).filter(pk__in=ids)
    titles = list(queryset.values_list("title", flat=True)[:3])
    deleted = queryset.count()
    if not deleted:
        messages.info(request, "这些事项已经不存在了。")
        return redirect(request.POST.get("next") or "events:todo")

    queryset.delete()
    sample = "、".join(f"《{title}》" for title in titles)
    suffix = "等" if deleted > len(titles) else ""
    messages.success(request, f"已删除 {deleted} 条事项：{sample}{suffix}。")
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
        return redirect("events:todo")
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
            confirm_event(event, reminder_offsets=form.offsets())
            messages.success(request, "已新建事项，待办和日程里都能看到。")
            return redirect("events:detail", pk=event.pk)
    else:
        form = ManualEventForm(prefix="new")
    return render(request, "events/create.html", {"form": form})
