from __future__ import annotations

from django.contrib import admin

from events.models import Event, EventField, EventMessage


class EventFieldInline(admin.TabularInline):
    model = EventField
    extra = 0
    fields = ("field", "value_text", "value_dt", "confidence", "is_current", "change_reason")


class EventMessageInline(admin.TabularInline):
    model = EventMessage
    extra = 0
    fields = ("message", "relation", "score")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = (
        "title", "owner", "category", "status", "todo_status", "deadline",
        "needs_review", "is_cancelled", "last_updated_at",
    )
    list_filter = ("status", "category", "todo_status", "needs_review", "is_cancelled")
    search_fields = ("title", "course", "location", "owner__username")
    date_hierarchy = "created_at"
    inlines = [EventFieldInline, EventMessageInline]


@admin.register(EventField)
class EventFieldAdmin(admin.ModelAdmin):
    list_display = ("event", "field", "value_text", "value_dt", "is_current", "confidence")
    list_filter = ("field", "is_current")
    search_fields = ("value_text", "event__title")
