"""轻量 JSON 接口。

前端默认服务端渲染，这里只提供轮询所需的少量接口：
* /api/notifications/  —— 派发并返回未读提醒（页面每 60 秒轮询一次；邮件也在这里发出）；
* /api/notifications/read/ —— 全部标记已读；
* /api/summary/ —— 首页角标数据。
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from reminders.models import InAppNotification
from reminders.services import dispatch_due, unread_count


@login_required
@require_GET
def notifications(request: HttpRequest) -> JsonResponse:
    # 带上 request：邮件里的「事项详情」链接要按用户当前访问的域名拼
    dispatch_due(user=request.user, request=request)
    items = InAppNotification.objects.filter(owner=request.user, read_at__isnull=True)[:20]
    return JsonResponse(
        {
            "unread": unread_count(request.user),
            "items": [
                {
                    "id": item.pk,
                    "title": item.title,
                    "body": item.body,
                    "level": item.level,
                    "event_id": item.event_id,
                    "created_at": item.created_at.isoformat(),
                }
                for item in items
            ],
        }
    )


@login_required
@require_POST
def mark_all_read(request: HttpRequest) -> JsonResponse:
    count = InAppNotification.objects.filter(owner=request.user, read_at__isnull=True).update(
        read_at=timezone.now()
    )
    return JsonResponse({"updated": count})
