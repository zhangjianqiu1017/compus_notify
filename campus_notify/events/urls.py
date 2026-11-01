from __future__ import annotations

from django.urls import path
from django.views.generic import RedirectView

from events import views

app_name = "events"

urlpatterns = [
    # 没有独立的「事项」栏目：待办（含「待确认」子栏目）/ 日程就是同一份数据的两面。
    # 老链接（/events/）不报 404，直接落到待办。
    path("", RedirectView.as_view(pattern_name="events:todo", permanent=False)),
    path("todo/", views.todo_list, name="todo"),
    path("calendar/", views.calendar_view, name="calendar"),
    # 手机上点日历格子落到这里：某一天的完整安排
    path("day/<int:year>/<int:month>/<int:day>/", views.day_view, name="day"),
    path("create/", views.create, name="create"),
    path("bulk-delete/", views.bulk_delete, name="bulk_delete"),
    path("review/<int:pk>/", views.review, name="review"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/delete/", views.delete_event, name="delete"),
    path("<int:pk>/complete/", views.complete, name="complete"),
    path("<int:pk>/reopen/", views.reopen, name="reopen"),
    path("<int:pk>/detach/<int:link_id>/", views.detach, name="detach"),
]
