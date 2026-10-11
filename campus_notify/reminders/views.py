from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from events.models import Event
from reminders.models import InAppNotification, Reminder
from reminders.services import (
    add_reminder as add_reminder_service,
    dispatch_due,
    offset_from_parts,
    recipient_for,
    toggle_reminder,
    update_reminder_offset,
)


def _read_offset(request: HttpRequest) -> int | None:
    """界面输入是「数量 + 单位」，例如 1 天、2 小时。"""
    return offset_from_parts(request.POST.get("amount"), request.POST.get("unit"))


def _back(request: HttpRequest, event_id: int) -> HttpResponse:
    """提醒的增改删都发生在其事项详情页，默认回到那里。"""
    return redirect(request.POST.get("next") or "events:detail", pk=event_id)


@login_required
@require_POST
def add_reminder(request: HttpRequest, event_id: int) -> HttpResponse:
    """添加一条提醒（FR-08）。"""
    event = get_object_or_404(Event, pk=event_id, owner=request.user)
    offset = _read_offset(request)
    if offset is None:
        messages.error(request, "请填写有效的提前时间（0 到 30 天）。")
        return _back(request, event.pk)
    try:
        reminder = add_reminder_service(event, offset)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _back(request, event.pk)
    messages.success(request, f"已添加提醒：{reminder.label}。")
    return _back(request, event.pk)


@login_required
@require_POST
def edit_reminder(request: HttpRequest, pk: int) -> HttpResponse:
    """修改某一条提醒（FR-08）。"""
    reminder = get_object_or_404(Reminder, pk=pk, owner=request.user)
    offset = _read_offset(request)
    if offset is None:
        messages.error(request, "请填写有效的提前时间（0 到 30 天）。")
        return _back(request, reminder.event_id)
    try:
        reminder = update_reminder_offset(reminder, offset)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _back(request, reminder.event_id)
    messages.success(request, f"提醒已改为：{reminder.label}。")
    return _back(request, reminder.event_id)


@login_required
@require_POST
def toggle(request: HttpRequest, pk: int) -> HttpResponse:
    reminder = get_object_or_404(Reminder, pk=pk, owner=request.user)
    enabled = request.POST.get("enabled") == "1"
    toggle_reminder(reminder, enabled)
    messages.success(request, f"提醒已{'开启' if enabled else '关闭'}。")
    return _back(request, reminder.event_id)


@login_required
@require_POST
def delete_reminder(request: HttpRequest, pk: int) -> HttpResponse:
    reminder = get_object_or_404(Reminder, pk=pk, owner=request.user)
    label = reminder.label or f"{reminder.offset_minutes} 分钟前"
    event_id = reminder.event_id
    reminder.delete()
    messages.success(request, f"已删除提醒：{label}。")
    return _back(request, event_id)


@login_required
def notification_list(request: HttpRequest) -> HttpResponse:
    dispatch_due(user=request.user, request=request)
    notifications = InAppNotification.objects.filter(owner=request.user).select_related("event")[:100]
    return render(
        request,
        "reminders/notifications.html",
        {
            "notifications": notifications,
            "reminder_email": recipient_for(request.user),
        },
    )


@login_required
@require_POST
def mark_read(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    queryset = InAppNotification.objects.filter(owner=request.user, read_at__isnull=True)
    if pk:
        queryset = queryset.filter(pk=pk)
    queryset.update(read_at=timezone.now())
    return redirect(request.POST.get("next") or "reminders:notifications")
