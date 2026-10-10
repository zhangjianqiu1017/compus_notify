from __future__ import annotations

from django.urls import path

from reminders import views

app_name = "reminders"

urlpatterns = [
    path("", views.reminder_list, name="list"),
    path("notifications/", views.notification_list, name="notifications"),
    path("notifications/<int:pk>/read/", views.mark_read, name="mark_read"),
    path("notifications/read-all/", views.mark_read, name="mark_all_read"),
    path("event/<int:event_id>/add/", views.add_reminder, name="add"),
    path("<int:pk>/edit/", views.edit_reminder, name="edit"),
    path("<int:pk>/toggle/", views.toggle, name="toggle"),
    path("<int:pk>/delete/", views.delete_reminder, name="delete"),
]
