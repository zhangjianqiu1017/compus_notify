from __future__ import annotations

from django.apps import AppConfig


class ExtractionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "extraction"
    verbose_name = "信息提取"
