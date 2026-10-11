"""事项层单测：确认门、待办状态、隔离、导出与删除、撤销合并。"""
from __future__ import annotations

import json
import re
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
        confirm_event(event, reminder_offsets=[1440, 120])
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.CONFIRMED)
        self.assertTrue(event.saved_as_todo)
        # 确认即同时进入日程，不需要用户在确认时选保存方式
        self.assertTrue(event.saved_as_calendar)
        self.assertEqual(event.todo_status, Event.TodoStatus.PENDING)
        self.assertEqual(event.reminders.count(), 2)
        labels = sorted(item.offset_minutes for item in event.reminders.all())
        self.assertEqual(labels, [120, 1440])

    def test_confirm_puts_event_in_both_todo_and_calendar(self):
        """一次确认，两个视图都能看到这条事项。"""
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        self.assertContains(self.client.get(reverse("events:todo")), "程序设计竞赛报名")
        calendar = self.client.get(reverse("events:calendar"), {"year": 2026, "month": 10})
        self.assertContains(calendar, "程序设计竞赛报名")

    def test_complete_and_reopen_todo(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[1440])
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

    def test_review_page_marks_evidence_in_original(self):
        """证据不再单列一栏，而是就地标在原文里：高亮片段 + 字段名标签。"""
        event = self._draft()
        response = self.client.get(reverse("events:review", args=[event.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<mark>10月20日</mark><span class="ev-label">截止时间</span>',
            html=False,
        )
        self.assertContains(
            response,
            '<mark>实验楼B302</mark><span class="ev-label">地点</span>',
            html=False,
        )
        # 「要求/材料」的证据是整行跨度，盖住了上面两个更精确的片段 → 让位，不参与高亮
        self.assertNotContains(response, '<span class="ev-label">要求/材料</span>')
        # 原「识别依据」卡片已删除（提示文案里仍会用到这个词，所以断言卡片标题）
        self.assertNotContains(response, "<h3>识别依据</h3>")
        # 确认页不该再出现「保存方式」勾选
        self.assertNotContains(response, "save_as_todo")

    def test_field_without_evidence_is_flagged(self):
        """定位不到证据的字段不能在页面上凭空消失：进「需要人工核对」。"""
        event = self._draft()
        field = event.current_field("location")
        field.source_message = None
        field.save(update_fields=["source_message"])

        response = self.client.get(reverse("events:review", args=[event.pk]))
        self.assertContains(response, "需要人工核对")
        self.assertContains(response, "未在原文中定位到证据")

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
            f"{prefix}-reminder_offsets": ["1440"],
        }
        # 表单里没有「保存为待办 / 日程」这类字段，确认后两者都该有
        response = self.client.post(reverse("events:review", args=[event.pk]), data)
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.CONFIRMED)
        self.assertTrue(event.saved_as_todo)
        self.assertTrue(event.saved_as_calendar)
        self.assertEqual(event.title, "程序设计竞赛报名（已核对）")
        # 用户把截止时间从 23:59 改成 18:00，提醒要跟着改
        self.assertEqual(event.deadline, aware(2026, 10, 22, 18, 0))
        self.assertEqual(event.reminders.first().remind_at, aware(2026, 10, 21, 18, 0))
        # 修改留痕（反馈闭环）
        from extraction.models import FieldEditLog

        self.assertTrue(FieldEditLog.objects.filter(owner=self.user, field="deadline").exists())

    def test_complete_via_view(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        response = self.client.post(reverse("events:complete", args=[event.pk]))
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.todo_status, Event.TodoStatus.DONE)

    def test_delete_event(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        self.client.post(reverse("events:delete", args=[event.pk]))
        self.assertFalse(Event.objects.filter(pk=event.pk).exists())

    def test_delete_from_todo_list_returns_to_todo(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[1440])
        response = self.client.post(
            reverse("events:delete", args=[event.pk]), {"next": reverse("events:todo")}
        )
        self.assertRedirects(response, reverse("events:todo"))
        self.assertFalse(Event.objects.filter(pk=event.pk).exists())
        # 提醒随事项一起删除
        self.assertFalse(Reminder.objects.filter(event_id=event.pk).exists())

    def test_delete_button_present_in_list_and_todo(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        delete_url = reverse("events:delete", args=[event.pk])
        for page in ("events:list", "events:todo"):
            response = self.client.get(reverse(page))
            self.assertContains(response, delete_url, msg_prefix=page)
            self.assertContains(response, "删除")

    def test_event_list_status_column_shows_todo_status_only(self):
        """「事项」页状态列只说这条事要做什么，不再显示「已确认」这种内部状态。"""
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        response = self.client.get(reverse("events:list"))
        self.assertContains(response, "待完成")
        self.assertNotContains(response, "已确认")

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
        confirm_event(pending, reminder_offsets=[])
        confirm_event(done, reminder_offsets=[])
        complete_todo(done)

        response = self.client.post(reverse("events:delete_completed"), follow=True)
        self.assertContains(response, "已删除 1 条已完成的待办")
        self.assertTrue(Event.objects.filter(pk=pending.pk).exists())
        self.assertFalse(Event.objects.filter(pk=done.pk).exists())

    def test_delete_completed_reports_when_nothing_to_delete(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        response = self.client.post(reverse("events:delete_completed"), follow=True)
        self.assertContains(response, "没有已完成的待办")
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())

    def test_calendar_view_contains_event(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        response = self.client.get(reverse("events:calendar"), {"year": 2026, "month": 10})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "程序设计竞赛报名")

    def test_edit_view_updates_and_reschedules(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[1440])
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
            f"{prefix}-reminder_offsets": ["1440"],
        }
        response = self.client.post(reverse("events:edit", args=[event.pk]), data)
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.deadline, aware(2026, 11, 1, 23, 59))
        self.assertEqual(event.reminders.first().remind_at, aware(2026, 10, 31, 23, 59))

    def test_manual_field_edit_keeps_history(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
        set_fields_from_form(self.user, event, {"location": "图书馆报告厅"}, source="edit")
        event.refresh_from_db()
        self.assertEqual(event.location, "图书馆报告厅")
        self.assertEqual(event.fields.filter(field="location", is_current=False).count(), 1)

    def test_clear_field_by_empty_value(self):
        event = self._draft()
        confirm_event(event, reminder_offsets=[])
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


class ClosedStateTests(TestCase):
    """已完成 / 已结束的事项整条变灰；没做完的过期待办仍是红色，不算结束。"""

    def setUp(self):
        self.user = make_user("closed")
        self.client.force_login(self.user)

    def _event(self, **kwargs):
        defaults = {"owner": self.user, "title": "期末讲座", "status": Event.Status.CONFIRMED}
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    def test_done_todo_is_closed_and_greyed_in_todo_list(self):
        event = self._event(
            saved_as_todo=True,
            todo_status=Event.TodoStatus.DONE,
            deadline=timezone.now() + timedelta(days=3),
        )
        self.assertTrue(event.is_closed)
        response = self.client.get(reverse("events:todo"), {"show": "all"})
        self.assertContains(response, '<tr class="is-closed">')
        self.assertContains(response, "已完成")

    def test_overdue_pending_todo_stays_open(self):
        event = self._event(
            saved_as_todo=True,
            todo_status=Event.TodoStatus.PENDING,
            deadline=timezone.now() - timedelta(hours=2),
        )
        self.assertTrue(event.overdue)
        self.assertFalse(event.is_closed)
        response = self.client.get(reverse("events:todo"), {"show": "all"})
        self.assertContains(response, "已过期")
        self.assertNotContains(response, '<tr class="is-closed">')

    def test_ended_event_is_closed_and_greyed_in_calendar(self):
        start = timezone.now() - timedelta(days=1)
        event = self._event(
            title="已经过去的讲座",
            saved_as_calendar=True,
            start_at=start,
            end_at=start + timedelta(hours=2),
        )
        self.assertTrue(event.is_ended)
        self.assertTrue(event.is_closed)
        local = timezone.localtime(start)
        response = self.client.get(
            reverse("events:calendar"), {"year": local.year, "month": local.month}
        )
        self.assertContains(response, "已经过去的讲座")
        self.assertContains(response, "cal-event  is-closed")

    def test_ended_event_shows_tag_in_event_list(self):
        event = self._event(
            title="已经结束的事项",
            start_at=timezone.now() - timedelta(days=2),
            end_at=timezone.now() - timedelta(days=2) + timedelta(hours=1),
        )
        response = self.client.get(reverse("events:list"))
        self.assertContains(response, '<tr class="is-closed">')
        self.assertContains(response, "已结束")

    def test_cancelled_event_is_closed(self):
        event = self._event(
            saved_as_todo=True,
            todo_status=Event.TodoStatus.PENDING,
            is_cancelled=True,
            deadline=timezone.now() + timedelta(days=1),
        )
        self.assertTrue(event.is_closed)
        response = self.client.get(reverse("events:todo"), {"show": "all"})
        self.assertContains(response, '<tr class="is-closed">')


class IsolationTests(TestCase):
    """NFR-12：仅本人可访问个人数据。"""

    def setUp(self):
        self.owner = make_user("owner")
        self.other = make_user("other")
        batch, _messages, _warnings = create_batch(self.owner, "【程序设计竞赛报名】\n请于10月20日前完成报名")
        self.event = process_batch(batch).drafts[0]
        confirm_event(self.event, reminder_offsets=[1440])

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
        confirm_event(self.event, reminder_offsets=[1440])

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


class TodoTimeColumnTests(TestCase):
    """待办列表的时间列不能只看截止时间：活动的开始时间同样要露出来。"""

    def setUp(self):
        self.user = make_user("todotime")
        self.client.force_login(self.user)

    def _event(self, title, **kwargs):
        defaults = {
            "owner": self.user,
            "title": title,
            "status": Event.Status.CONFIRMED,
            "saved_as_todo": True,
            "todo_status": Event.TodoStatus.PENDING,
        }
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    def _body(self):
        return self.client.get(reverse("events:todo")).content.decode("utf-8")

    @staticmethod
    def _shown(value):
        return timezone.localtime(value).strftime("%m-%d %H:%M")

    def test_activity_without_deadline_shows_start_time(self):
        """只有开始时间的活动（讲座、比赛）不能再显示「未填」。"""
        event = self._event("校园歌手大赛初赛", start_at=timezone.now() + timedelta(days=2))
        body = self._body()
        self.assertIn(f"开始 {self._shown(event.start_at)}", body)
        self.assertNotIn("未填", body)

    def test_event_with_both_times_shows_both(self):
        start = timezone.now() + timedelta(days=1, hours=2)
        deadline = timezone.now() + timedelta(days=9)
        self._event("程序设计竞赛", start_at=start, deadline=deadline)
        body = self._body()
        self.assertIn(f"截止 {self._shown(deadline)}", body)
        self.assertIn(f"开始 {self._shown(start)}", body)

    def test_start_only_activity_is_not_pushed_to_the_bottom(self):
        """没填截止时间的事项按开始时间参与排序，而不是沉到最后。"""
        self._event("只有开始时间的活动", start_at=timezone.now() + timedelta(days=1))
        self._event("只有截止时间的作业", deadline=timezone.now() + timedelta(days=4))
        body = self._body()
        self.assertLess(body.index("只有开始时间的活动"), body.index("只有截止时间的作业"))

    def test_both_times_sorted_by_deadline(self):
        """两个时间都填了时以截止时间为准，和事项列表（order_by_time）保持一致。"""
        now = timezone.now()
        self._event("晚截止的报名", start_at=now + timedelta(days=1), deadline=now + timedelta(days=10))
        self._event("早截止的作业", deadline=now + timedelta(days=3))
        body = self._body()
        self.assertLess(body.index("早截止的作业"), body.index("晚截止的报名"))

    def test_past_start_time_of_ongoing_activity_is_highlighted(self):
        """活动已开始但还没结束：红点要落在开始时间上，而不是整列都没有时间点。"""
        now = timezone.now()
        self._event(
            "正在进行的讲座",
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=1),
        )
        body = self._body()
        self.assertIn('class="overdue">开始', body)


class EventListOrderTests(TestCase):
    """US-10：事项列表按时间排序——有截止时间的看截止，没有的看开始。"""

    def setUp(self):
        self.user = make_user("listorder")
        self.client.force_login(self.user)

    def _event(self, title, **kwargs):
        defaults = {
            "owner": self.user,
            "title": title,
            "status": Event.Status.CONFIRMED,
            "saved_as_todo": True,
            "todo_status": Event.TodoStatus.PENDING,
        }
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    def _body(self):
        response = self.client.get(reverse("events:list"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def _row_titles(self, body):
        """按渲染顺序取出表格里的标题。

        不能直接 `body.index(标题)`：筛选项里的分类标签（作业、考试）会先命中。
        """
        return re.findall(r'<a class="title-link" href="[^"]*">([^<]+)</a>', body)

    def _order(self, body, *titles):
        rendered = self._row_titles(body)
        return [rendered.index(title) for title in titles]

    def test_deadline_and_start_only_items_interleave_by_time(self):
        """报名类看截止、活动类看开始，两种事项在同一把时间尺子上排。"""
        now = timezone.now()
        self._event("晚一点的讲座", start_at=now + timedelta(days=1))
        self._event("更晚的作业", deadline=now + timedelta(days=3))
        self._event("最晚的比赛", start_at=now + timedelta(days=5))
        body = self._body()
        lecture, homework, contest = self._order(body, "晚一点的讲座", "更晚的作业", "最晚的比赛")
        self.assertLess(lecture, homework)
        self.assertLess(homework, contest)

    def test_item_with_both_times_sorts_by_deadline(self):
        """既有开始又有截止时以截止为准：开始时间不参与排序。"""
        now = timezone.now()
        self._event(
            "下月截止的报名", start_at=now + timedelta(days=1), deadline=now + timedelta(days=20)
        )
        self._event("三天后截止的作业", deadline=now + timedelta(days=3))
        body = self._body()
        later, sooner = self._order(body, "下月截止的报名", "三天后截止的作业")
        self.assertLess(sooner, later)

    def test_items_without_any_time_go_last(self):
        """两个时间都没填的事项排最后，而不是插在已排好的时间轴中间。"""
        now = timezone.now()
        self._event("没填时间的事项")
        self._event("最晚的截止", deadline=now + timedelta(days=30))
        body = self._body()
        undated, dated = self._order(body, "没填时间的事项", "最晚的截止")
        self.assertLess(dated, undated)

    def test_list_does_not_fall_back_to_last_updated(self):
        """排序只看时间，不再被「最近更新」打乱。"""
        now = timezone.now()
        old = self._event("很久以前改过的截止事项", deadline=now + timedelta(days=2))
        fresh = self._event("刚刚改过的截止事项", deadline=now + timedelta(days=9))
        Event.objects.filter(pk=old.pk).update(last_updated_at=now - timedelta(days=30))
        Event.objects.filter(pk=fresh.pk).update(last_updated_at=now)
        body = self._body()
        earlier, later = self._order(body, "很久以前改过的截止事项", "刚刚改过的截止事项")
        self.assertLess(earlier, later)

    def test_list_still_shows_both_times(self):
        """排序变了，时间列的信息量不能少：截止和开始都要露出来。"""
        now = timezone.now()
        self._event(
            "既有开始又有截止", start_at=now + timedelta(days=1), deadline=now + timedelta(days=9)
        )
        body = self._body()
        self.assertIn(f"截止 {timezone.localtime(now + timedelta(days=9)).strftime('%m-%d %H:%M')}", body)
        self.assertIn(f"开始 {timezone.localtime(now + timedelta(days=1)).strftime('%m-%d %H:%M')}", body)
