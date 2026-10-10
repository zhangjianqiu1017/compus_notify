from __future__ import annotations

from django.contrib import admin

from accounts.models import UserPreference


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "store_raw_messages", "warn_sensitive", "updated_at")
    search_fields = ("user__username",)
