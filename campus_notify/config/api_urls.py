"""JSON 接口路由。"""
from __future__ import annotations

from django.urls import path

from core import api, views

urlpatterns = [
    path("summary/", views.api_summary, name="api_summary"),
    path("notifications/", api.notifications, name="api_notifications"),
    path("notifications/read/", api.mark_all_read, name="api_notifications_read"),
]
