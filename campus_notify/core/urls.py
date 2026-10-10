from __future__ import annotations

from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("search/", views.search_view, name="search"),
    path("privacy/", views.privacy, name="privacy"),
    path("data/", views.data_management, name="data"),
    path("data/export.json", views.export_json, name="export_json"),
    path("data/export.ics", views.export_ics, name="export_ics"),
    path("data/purge-raw/", views.purge_raw, name="purge_raw"),
    path("data/delete-all/", views.delete_all, name="delete_all"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
]
