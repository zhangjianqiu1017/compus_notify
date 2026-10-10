from __future__ import annotations

from django.contrib import admin

from reminders.models import InAppNotification, Reminder


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("event", "owner", "anchor_kind", "offset_minutes", "remind_at", "status", "is_enabled")
    list_filter = ("status", "anchor_kind", "is_enabled")
    search_fields = ("event__title", "owner__username")


@admin.register(InAppNotification)
class InAppNotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "level", "created_at", "read_at")
    list_filter = ("level",)
    search_fields = ("title", "owner__username")
