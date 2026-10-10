"""项目路由。"""
from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("ingest/", include("ingest.urls")),
    path("events/", include("events.urls")),
    path("reminders/", include("reminders.urls")),
    path("api/", include("config.api_urls")),
    path("", include("core.urls")),
]
