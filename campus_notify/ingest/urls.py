from __future__ import annotations

from django.urls import path

from ingest import views

app_name = "ingest"

urlpatterns = [
    path("", views.import_view, name="import"),
    path("share/", views.share_target, name="share"),
    path("batch/<int:pk>/", views.batch_detail, name="batch"),
]
