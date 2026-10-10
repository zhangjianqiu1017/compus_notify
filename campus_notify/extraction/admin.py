from __future__ import annotations

from django.contrib import admin

from extraction.models import ExtractionRun, FieldEditLog


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "provider", "status", "message_count",
        "event_count", "duration_ms", "created_at",
    )
    list_filter = ("provider", "status")
    search_fields = ("owner__username",)


@admin.register(FieldEditLog)
class FieldEditLogAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "event_title", "field", "source", "created_at")
    list_filter = ("field", "source")
    search_fields = ("event_title", "owner__username")
