from __future__ import annotations

from django.urls import path

from events import views

app_name = "events"

urlpatterns = [
    path("", views.event_list, name="list"),
    path("todo/", views.todo_list, name="todo"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("create/", views.create, name="create"),
    path("bulk-delete/", views.bulk_delete, name="bulk_delete"),
    path("delete-completed/", views.delete_completed, name="delete_completed"),
    path("review/", views.review_list, name="review_list"),
    path("review/<int:pk>/", views.review, name="review"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/delete/", views.delete_event, name="delete"),
    path("<int:pk>/complete/", views.complete, name="complete"),
    path("<int:pk>/reopen/", views.reopen, name="reopen"),
    path("<int:pk>/detach/<int:link_id>/", views.detach, name="detach"),
]
