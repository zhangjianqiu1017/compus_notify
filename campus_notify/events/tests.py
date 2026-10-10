"""事项层单测：确认门、待办状态、隔离、导出与删除、撤销合并。"""
from __future__ import annotations

import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event
from events.services import complete_todo, confirm_event, reopen_todo, set_fields_from_form
from extraction.pipeline import process_batch
from ingest.services import create_batch
from reminders.models import Reminder
from tests_helpers import aware, make_user


class EventFlowTests(TestCase):
    def setUp(self):
        self.user = make_user("flow")
        self.client.force_login(self.user)

    def _draft(self, raw="【程序设计竞赛报名】\n请于10月20日前完成报名，报名链接：https://example.edu/contest\n地点：实验楼B302"):
        batch, _messages, _warnings = create_batch(self.user, raw)
        result = process_batch(batch)
        return result.drafts[0]

    def test_confirm_creates_todo_and_reminders(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[1440, 120])
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.CONFIRMED)
        self.assertTrue(event.saved_as_todo)
        self.assertEqual(event.todo_status, Event.TodoStatus.PENDING)
        self.assertEqual(event.reminders.count(), 2)
        labels = sorted(item.offset_minutes for item in event.reminders.all())
        self.assertEqual(labels, [120, 1440])

    def test_complete_and_reopen_todo(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[1440])
        complete_todo(event)
        event.refresh_from_db()
        self.assertEqual(event.todo_status, Event.TodoStatus.DONE)
        self.assertIsNotNone(event.done_at)
        self.assertTrue(event.is_open_todo is False)
        # 完成后提醒应被停用，避免用户已做完还被提醒
        self.assertFalse(event.reminders.filter(status="scheduled").exists())

        reopen_todo(event)
        event.refresh_from_db()
        self.assertEqual(event.todo_status, Event.TodoStatus.PENDING)
        self.assertIsNone(event.done_at)

    def test_draft_is_not_in_todo_list_before_confirmation(self):
        self._draft()
        response = self.client.get(reverse("events:todo"))
        self.assertNotContains(response, "程序设计竞赛报名")
        response = self.client.get(reverse("events:review_list"))
        self.assertContains(response, "程序设计竞赛报名")

    def test_review_page_renders_evidence(self):
        event = self._draft()
        response = self.client.get(reverse("events:review", args=[event.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "识别依据")
        self.assertContains(response, "<mark>10月20日</mark>", html=False)

    def test_review_post_confirms_with_edited_values(self):
        event = self._draft()
        prefix = f"e{event.pk}"
        data = {
            f"{prefix}-title": "程序设计竞赛报名（已核对）",
            f"{prefix}-category": "contest",
            f"{prefix}-course": "软件工程",
            f"{prefix}-location": "实验楼B302",
            f"{prefix}-organizer": "",
            f"{prefix}-link": "https://example.edu/contest",
            f"{prefix}-contact": "",
            f"{prefix}-requirement": "",
            f"{prefix}-deadline": "2026-10-22T18:00",
            f"{prefix}-start_at": "",
            f"{prefix}-end_at": "",
            f"{prefix}-save_as_todo": "on",
            f"{prefix}-reminder_offsets": ["1440"],
        }
        response = self.client.post(reverse("events:review", args=[event.pk]), data)
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.CONFIRMED)
        self.assertEqual(event.title, "程序设计竞赛报名（已核对）")
        # 用户把截止时间从 23:59 改成 18:00，提醒要跟着改
        self.assertEqual(event.deadline, aware(2026, 10, 22, 18, 0))
        self.assertEqual(event.reminders.first().remind_at, aware(2026, 10, 21, 18, 0))
        # 修改留痕（反馈闭环）
        from extraction.models import FieldEditLog

        self.assertTrue(FieldEditLog.objects.filter(owner=self.user, field="deadline").exists())

    def test_complete_via_view(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[])
        response = self.client.post(reverse("events:complete", args=[event.pk]))
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.todo_status, Event.TodoStatus.DONE)

    def test_delete_event(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[])
        self.client.post(reverse("events:delete", args=[event.pk]))
        self.assertFalse(Event.objects.filter(pk=event.pk).exists())

    def test_delete_from_todo_list_returns_to_todo(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[1440])
        response = self.client.post(
            reverse("events:delete", args=[event.pk]), {"next": reverse("events:todo")}
        )
        self.assertRedirects(response, reverse("events:todo"))
        self.assertFalse(Event.objects.filter(pk=event.pk).exists())
        # 提醒随事项一起删除
        self.assertFalse(Reminder.objects.filter(event_id=event.pk).exists())

    def test_delete_button_present_in_list_and_todo(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[])
        delete_url = reverse("events:delete", args=[event.pk])
        for page in ("events:list", "events:todo"):
            response = self.client.get(reverse(page))
            self.assertContains(response, delete_url, msg_prefix=page)
            self.assertContains(response, "删除")

    def test_bulk_delete_removes_selected(self):
        keep = self._draft("【程序设计竞赛报名】\n请于10月20日前完成报名")
        first = self._draft("【高等数学作业】\n请于10月15日前提交")
        second = self._draft("【校园歌手大赛】\n报名截止10月30日")
        response = self.client.post(
            reverse("events:bulk_delete"),
            {"ids": [str(first.pk), str(second.pk)], "next": reverse("events:list")},
        )
        self.assertRedirects(response, reverse("events:list"))
        remaining = set(Event.objects.values_list("pk", flat=True))
        self.assertIn(keep.pk, remaining)
        self.assertNotIn(first.pk, remaining)
        self.assertNotIn(second.pk, remaining)

    def test_bulk_delete_without_selection_warns(self):
        event = self._draft()
        response = self.client.post(reverse("events:bulk_delete"), {}, follow=True)
        self.assertContains(response, "请先勾选")
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())

    def test_bulk_delete_cannot_touch_other_users_data(self):
        """勾选别人的 id 也不该删掉——查询必须按所有者收口。"""
        mine = self._draft()
        other = make_user("victim")
        batch, _messages, _warnings = create_batch(other, "【别人的通知】\n请于11月1日前提交")
        victim_event = process_batch(batch).drafts[0]

        response = self.client.post(
            reverse("events:bulk_delete"),
            {"ids": [str(mine.pk), str(victim_event.pk)]},
            follow=True,
        )
        self.assertContains(response, "已删除 1 条")
        self.assertFalse(Event.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Event.objects.filter(pk=victim_event.pk).exists())

    def test_delete_completed_todos_only(self):
        pending = self._draft("【程序设计竞赛报名】\n请于10月20日前完成报名")
        done = self._draft("【高等数学作业】\n请于10月15日前提交")
        confirm_event(pending, as_todo=True, as_calendar=False, reminder_offsets=[])
        confirm_event(done, as_todo=True, as_calendar=False, reminder_offsets=[])
        complete_todo(done)

        response = self.client.post(reverse("events:delete_completed"), follow=True)
        self.assertContains(response, "已删除 1 条已完成的待办")
        self.assertTrue(Event.objects.filter(pk=pending.pk).exists())
        self.assertFalse(Event.objects.filter(pk=done.pk).exists())

    def test_delete_completed_reports_when_nothing_to_delete(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[])
        response = self.client.post(reverse("events:delete_completed"), follow=True)
        self.assertContains(response, "没有已完成的待办")
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())

    def test_calendar_view_contains_event(self):
        event = self._draft()
        confirm_event(event, as_todo=False, as_calendar=True, reminder_offsets=[])
        response = self.client.get(reverse("events:calendar"), {"year": 2026, "month": 10})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "程序设计竞赛报名")

    def test_edit_view_updates_and_reschedules(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[1440])
        prefix = f"e{event.pk}"
        data = {
            f"{prefix}-title": event.title,
            f"{prefix}-category": event.category,
            f"{prefix}-course": "",
            f"{prefix}-location": "实验楼B302",
            f"{prefix}-organizer": "",
            f"{prefix}-link": "",
            f"{prefix}-contact": "",
            f"{prefix}-requirement": "",
            f"{prefix}-deadline": "2026-11-01T23:59",
            f"{prefix}-start_at": "",
            f"{prefix}-end_at": "",
            f"{prefix}-save_as_todo": "on",
            f"{prefix}-reminder_offsets": ["1440"],
        }
        response = self.client.post(reverse("events:edit", args=[event.pk]), data)
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.deadline, aware(2026, 11, 1, 23, 59))
        self.assertEqual(event.reminders.first().remind_at, aware(2026, 10, 31, 23, 59))

    def test_manual_field_edit_keeps_history(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[])
        set_fields_from_form(self.user, event, {"location": "图书馆报告厅"}, source="edit")
        event.refresh_from_db()
        self.assertEqual(event.location, "图书馆报告厅")
        self.assertEqual(event.fields.filter(field="location", is_current=False).count(), 1)

    def test_clear_field_by_empty_value(self):
        event = self._draft()
        confirm_event(event, as_todo=True, as_calendar=False, reminder_offsets=[])
        set_fields_from_form(self.user, event, {"link": ""}, source="edit")
        event.refresh_from_db()
        self.assertEqual(event.link, "")
        self.assertIsNone(event.current_field("link"))

    def test_detach_message_creates_new_draft(self):
        raw = (
            "张三 2026-10-08 09:12\n"
            "【程序设计竞赛报名】\n请于10月20日前完成报名\n"
            "张三 2026-10-08 09:15\n更正：报名地点改到实验楼B302"
        )
        batch, _messages, _warnings = create_batch(self.user, raw)
        result = process_batch(batch)
        event = result.drafts[0]
        self.assertEqual(event.links.count(), 2)
        link = event.links.exclude(relation="primary").first() or event.links.last()
        response = self.client.post(reverse("events:detach", args=[event.pk, link.pk]))
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.links.count(), 1)


class IsolationTests(TestCase):
    """NFR-12：仅本人可访问个人数据。"""

    def setUp(self):
        self.owner = make_user("owner")
        self.other = make_user("other")
        batch, _messages, _warnings = create_batch(self.owner, "【程序设计竞赛报名】\n请于10月20日前完成报名")
        self.event = process_batch(batch).drafts[0]
        confirm_event(self.event, as_todo=True, as_calendar=True, reminder_offsets=[1440])

    def test_other_user_cannot_open_detail(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("events:detail", args=[self.event.pk]))
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_complete_or_delete(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("events:complete", args=[self.event.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("events:delete", args=[self.event.pk])).status_code, 404)

    def test_other_user_list_is_empty(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("events:list"))
        self.assertNotContains(response, "程序设计竞赛报名")

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("events:detail", args=[self.event.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])


class SearchAndExportTests(TestCase):
    def setUp(self):
        self.user = make_user("searcher")
        self.client.force_login(self.user)
        batch, _messages, _warnings = create_batch(
            self.user,
            "【程序设计竞赛报名】\n请于10月20日前完成报名，报名链接：https://example.edu/contest\n地点：实验楼B302",
        )
        self.event = process_batch(batch).drafts[0]
        confirm_event(self.event, as_todo=True, as_calendar=True, reminder_offsets=[1440])

    def test_search_hits_event_and_raw_message(self):
        response = self.client.get(reverse("core:search"), {"q": "程序设计"})
        self.assertContains(response, "程序设计竞赛报名")
        response = self.client.get(reverse("core:search"), {"q": "实验楼"})
        self.assertContains(response, "实验楼B302")

    def test_search_without_keyword_is_empty_state(self):
        response = self.client.get(reverse("core:search"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "输入关键词开始检索")

    def test_export_json(self):
        response = self.client.get(reverse("core:export_json"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["title"], "程序设计竞赛报名")
        self.assertTrue(payload["events"][0]["fields"])
        self.assertTrue(payload["events"][0]["sources"])

    def test_export_ics(self):
        response = self.client.get(reverse("core:export_ics"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("BEGIN:VEVENT", body)
        self.assertIn("UID:event-", body)

    def test_purge_raw_keeps_events(self):
        response = self.client.post(reverse("core:purge_raw"))
        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertTrue(self.event.fields.exists())
        self.assertTrue(self.event.links.first().message.raw_discarded)

    def test_delete_all_data(self):
        from ingest.models import Message

        response = self.client.post(reverse("core:delete_all"), {"confirm": "清空"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Event.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)

    def test_delete_all_requires_confirmation_text(self):
        response = self.client.post(reverse("core:delete_all"), {"confirm": "随便"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Event.objects.count(), 1)

    def test_home_page_renders(self):
        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "今天要处理的事")

    def test_manifest_is_served(self):
        response = self.client.get(reverse("core:manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("share_target", response.content.decode("utf-8"))
