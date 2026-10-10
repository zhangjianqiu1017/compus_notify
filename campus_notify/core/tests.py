"""页面渲染冒烟测试：确保每个页面都能在真实数据下渲染出来（模板错误很难靠单测发现）。"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from events.services import confirm_event
from extraction.pipeline import process_batch
from ingest.models import Message
from ingest.services import create_batch
from tests_helpers import make_user

RAW = (
    "张三 2026-10-08 09:12\n"
    "【程序设计竞赛报名】\n"
    "请于10月20日前完成报名，报名链接：https://example.edu/contest\n"
    "地点：实验楼B302\n"
    "联系人：李老师\n"
    "要求：请携带学生证\n"
    "张三 2026-10-08 09:15\n"
    "更正：报名地点改到实验楼B303"
)


class AllPagesRenderTests(TestCase):
    def setUp(self):
        self.user = make_user("pages")
        self.client.force_login(self.user)
        self.batch, _messages, _warnings = create_batch(self.user, RAW)
        self.result = process_batch(self.batch)
        self.draft = self.result.drafts[0]
        confirm_event(self.draft, as_todo=True, as_calendar=True, reminder_offsets=[1440, 120])

    def test_all_pages_render(self):
        pages = [
            reverse("core:home"),
            reverse("core:search") + "?q=程序设计",
            reverse("core:privacy"),
            reverse("core:data"),
            reverse("core:manifest"),
            reverse("accounts:profile"),
            reverse("ingest:import"),
            reverse("ingest:batch", args=[self.batch.pk]),
            reverse("events:list"),
            reverse("events:todo"),
            reverse("events:todo") + "?show=done",
            reverse("events:calendar") + "?year=2026&month=10",
            reverse("events:create"),
            reverse("events:review_list"),
            reverse("events:detail", args=[self.draft.pk]),
            reverse("events:edit", args=[self.draft.pk]),
            reverse("reminders:list"),
            reverse("reminders:notifications"),
            reverse("api_summary"),
            reverse("api_notifications"),
            reverse("accounts:login"),
            reverse("accounts:register"),
            reverse("accounts:password_reset"),
            reverse("accounts:password_reset_confirm"),
        ]
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(response.status_code, (200, 302), f"{url} 返回 {response.status_code}")

    def test_no_template_syntax_leaks_into_pages(self):
        """回归：模板注释/标签不能漏成页面上的可见文字。

        Django 的 {# #} 只支持单行；写成多行就不会被当成注释，会原样渲染给用户。
        这类问题在 HTTP 测试里看不出来（页面照样 200），所以在这里统一兜住。
        """
        pages = [
            reverse("core:home"),
            reverse("ingest:import"),
            reverse("events:list"),
            reverse("events:todo"),
            reverse("events:calendar"),
            reverse("events:create"),
            reverse("events:review_list"),
            reverse("events:detail", args=[self.draft.pk]),
            reverse("events:edit", args=[self.draft.pk]),
            reverse("reminders:list"),
            reverse("reminders:notifications"),
            reverse("core:data"),
            reverse("core:privacy"),
            reverse("core:search"),
            reverse("accounts:profile"),
            reverse("accounts:register"),
            reverse("accounts:password_reset"),
            reverse("accounts:password_reset_confirm"),
        ]
        for url in pages:
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                for token in ("{#", "#}", "{%", "%}", "{{", "}}"):
                    self.assertNotIn(token, html, f"{url} 页面上漏出了模板语法 {token}")

    def test_review_page_for_confirmed_event_redirects(self):
        response = self.client.get(reverse("events:review", args=[self.draft.pk]))
        self.assertEqual(response.status_code, 302)

    def test_import_post_flow_redirects_to_review(self):
        response = self.client.post(
            reverse("ingest:import"),
            {"text": "【高等数学作业】请于11月5日前提交第三章习题", "source": "paste"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("events:review", args=[1]).rsplit("/", 2)[0], response["Location"])

    def test_share_target_stores_text_and_redirects(self):
        response = self.client.post(
            reverse("ingest:share"),
            {"text": "【讲座通知】10月25日 14:00 在图书馆报告厅举办", "title": "分享的通知"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("ingest:import"))
        follow = self.client.get(reverse("ingest:import"))
        self.assertContains(follow, "图书馆报告厅")

    def test_duplicate_import_shows_where_it_came_from(self):
        """重复导入要有出口：说清撞上哪一条，并提供「仍然重新识别」。"""
        self.client.post(
            reverse("ingest:import"), {"text": RAW, "source": "paste"}
        )
        response = self.client.post(reverse("ingest:import"), {"text": RAW, "source": "paste"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "这段内容已经导入过了")
        self.assertContains(response, "仍然重新识别")
        self.assertContains(response, "程序设计竞赛报名")  # 关联到已有事项

        before = Message.objects.filter(owner=self.user).count()
        response = self.client.post(
            reverse("ingest:import"), {"text": RAW, "source": "paste", "reuse": "1"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Message.objects.filter(owner=self.user).count(), before)

    def test_manual_create_flow(self):
        response = self.client.post(
            reverse("events:create"),
            {
                "new-title": "手动录入的会议",
                "new-category": "activity",
                "new-course": "",
                "new-location": "行政楼 302",
                "new-organizer": "",
                "new-link": "",
                "new-contact": "",
                "new-requirement": "",
                "new-deadline": "2026-11-11T09:00",
                "new-start_at": "",
                "new-end_at": "",
                "new-save_as_todo": "on",
                "new-reminder_offsets": ["1440"],
            },
        )
        self.assertEqual(response.status_code, 302)
        from events.models import Event

        event = Event.objects.get(title="手动录入的会议")
        self.assertEqual(event.status, Event.Status.CONFIRMED)
        self.assertEqual(event.reminders.count(), 1)

    def test_manual_create_requires_time(self):
        response = self.client.post(
            reverse("events:create"),
            {
                "new-title": "没有时间的会议",
                "new-category": "activity",
                "new-course": "",
                "new-location": "",
                "new-organizer": "",
                "new-link": "",
                "new-contact": "",
                "new-requirement": "",
                "new-deadline": "",
                "new-start_at": "",
                "new-end_at": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请至少填写截止时间或开始时间")
