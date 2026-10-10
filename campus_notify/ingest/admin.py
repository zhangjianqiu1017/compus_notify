from __future__ import annotations

from django.contrib import admin

from ingest.models import Batch, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("seq", "sender", "sent_at", "has_sensitive", "raw_discarded", "raw_text")


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "source", "created_at", "stats")
    list_filter = ("source",)
    search_fields = ("owner__username",)
    inlines = [MessageInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "seq", "sender", "has_sensitive", "raw_discarded", "created_at")
    list_filter = ("has_sensitive", "raw_discarded")
    search_fields = ("sender", "raw_text", "owner__username")
