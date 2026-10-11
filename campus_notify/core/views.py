from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.services import (
    count_user_data,
    delete_all_user_data,
    events_to_ics,
    export_payload,
    purge_raw_messages,
    search,
    upcoming,
)
from events.models import Event
from reminders.services import dispatch_due, unread_count


@login_required
def home(request: HttpRequest) -> HttpResponse:
    # 打开首页即派发到点提醒（NFR-04：离线恢复后补偿），并及时发出邮件提醒
    dispatch_due(user=request.user, request=request)
    drafts = (
        Event.objects.for_user(request.user)
        .filter(status=Event.Status.DRAFT)
        .order_by("-last_updated_at")[:5]
    )
    data = upcoming(request.user, days=7)
    return render(
        request,
        "core/home.html",
        {
            "drafts": drafts,
            "draft_count": Event.objects.for_user(request.user).filter(status=Event.Status.DRAFT).count(),
            "stats": count_user_data(request.user),
            "now": timezone.now(),
            **data,
        },
    )


@login_required
@require_GET
def search_view(request: HttpRequest) -> HttpResponse:
    keyword = request.GET.get("q", "")
    results = search(request.user, keyword)
    return render(request, "core/search.html", results)


@login_required
@require_GET
def privacy(request: HttpRequest) -> HttpResponse:
    """NFR-17：明确告知是否上传、存储周期、用途与删除途径。"""
    from accounts.models import UserPreference

    preference = UserPreference.for_user(request.user)
    return render(
        request,
        "core/privacy.html",
        {"preference": preference, "stats": count_user_data(request.user)},
    )


@login_required
@require_GET
def data_management(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "core/data.html",
        {"stats": count_user_data(request.user), "unread": unread_count(request.user)},
    )


@login_required
@require_GET
def export_json(request: HttpRequest) -> HttpResponse:
    payload = export_payload(request.user)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    response = HttpResponse(body, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="campus-notify-export.json"'
    return response


@login_required
@require_GET
def export_ics(request: HttpRequest) -> HttpResponse:
    events = (
        Event.objects.for_user(request.user)
        .filter(status=Event.Status.CONFIRMED)
        .prefetch_related("fields")
        .order_by("start_at", "deadline")
    )
    body = events_to_ics(events)
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="campus-notify.ics"'
    return response


@login_required
@require_POST
def purge_raw(request: HttpRequest) -> HttpResponse:
    count = purge_raw_messages(request.user)
    messages.success(request, f"已删除 {count} 条原文，结构化事项保持不变。")
    return redirect("core:data")


@login_required
@require_POST
def delete_all(request: HttpRequest) -> HttpResponse:
    if (request.POST.get("confirm") or "").strip() != "清空":
        messages.error(request, "请输入“清空”两个字以确认删除全部数据。")
        return redirect("core:data")
    counts = delete_all_user_data(request.user)
    messages.success(request, f"已删除全部数据（共 {sum(counts.values())} 条记录）。")
    return redirect("core:home")


@require_GET
def manifest(request: HttpRequest) -> HttpResponse:
    """PWA 清单：让 Web 版本也能出现在系统分享列表里（FR-02 系统分享导入）。"""
    return render(request, "manifest.webmanifest", content_type="application/manifest+json")


@login_required
@require_GET
def api_summary(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "stats": count_user_data(request.user),
            "unread": unread_count(request.user),
            "server_time": timezone.now().isoformat(),
        }
    )
