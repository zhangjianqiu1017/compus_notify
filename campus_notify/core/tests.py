"""页面渲染冒烟测试：确保每个页面都能在真实数据下渲染出来（模板错误很难靠单测发现）。"""
from __future__ import annotations

from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.templatetags.ui import mark_evidence
from events.models import Event
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
        confirm_event(self.draft, reminder_offsets=[1440, 120])

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
            reverse("reminders:notifications"),
            reverse("api_summary"),
            reverse("api_notifications"),
            reverse("accounts:login"),
            reverse("accounts:register"),
            reverse("accounts:code_login"),
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
            reverse("reminders:notifications"),
            reverse("core:data"),
            reverse("core:privacy"),
            reverse("core:search"),
            reverse("accounts:profile"),
            reverse("accounts:register"),
            reverse("accounts:code_login"),
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
        """重复导入要有出口：一句话说清结果，并提供「仍然重新识别」。

        提示只保留结论，不再铺开原文预览、事项清单和去重说明——那些是噪音。
        """
        self.client.post(
            reverse("ingest:import"), {"text": RAW, "source": "paste"}
        )
        response = self.client.post(reverse("ingest:import"), {"text": RAW, "source": "paste"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "这段内容已经导入过了")
        self.assertContains(response, "没有新建原文")
        self.assertContains(response, "仍然重新识别")
        # 细节不展开：没有原文/事项清单，也没有去重原理说明
        self.assertNotContains(response, "这是按原文内容去重")
        self.assertNotContains(response, "暂无对应事项")

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
                "new-reminder_offsets": ["1440"],
            },
        )
        self.assertEqual(response.status_code, 302)
        from events.models import Event

        event = Event.objects.get(title="手动录入的会议")
        self.assertEqual(event.status, Event.Status.CONFIRMED)
        # 手动新建同样一次进待办 + 日程
        self.assertTrue(event.saved_as_todo)
        self.assertTrue(event.saved_as_calendar)
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


class HomeTimelineTests(TestCase):
    """首页时间轴：确认后的事项既是待办也是日程，所以一条只该出现一次。"""

    def setUp(self):
        self.user = make_user("home")
        self.client.force_login(self.user)

    def _event(self, title, **kwargs):
        defaults = {
            "owner": self.user,
            "title": title,
            "status": Event.Status.CONFIRMED,
            "saved_as_todo": True,
            "saved_as_calendar": True,
            "todo_status": Event.TodoStatus.PENDING,
        }
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    def test_event_with_both_times_appears_once(self):
        start = timezone.now() + timedelta(days=1)
        self._event(
            "既有截止又有开始的事项",
            start_at=start,
            deadline=timezone.now() + timedelta(days=2),
        )
        response = self.client.get(reverse("core:home"))
        body = response.content.decode()
        self.assertEqual(body.count("既有截止又有开始的事项"), 1)
        self.assertContains(response, "最近 7 天")
        # 排序与标签都取最近的那个时间点：这里是「开始」
        row = response.context["timeline"][0]
        self.assertEqual(row["kind"], "start_at")
        self.assertEqual(row["anchor"], start)

    def test_overdue_is_listed_once_in_its_own_card(self):
        self._event("过期还没做的作业", deadline=timezone.now() - timedelta(hours=3))
        response = self.client.get(reverse("core:home"))
        body = response.content.decode()
        self.assertContains(response, "已过截止时间")
        self.assertEqual(body.count("过期还没做的作业"), 1)
        self.assertEqual(response.context["overdue"][0].title, "过期还没做的作业")
        self.assertEqual(response.context["stats"]["open_todos"], 1)

    def test_done_and_cancelled_items_stay_off_the_homepage(self):
        self._event(
            "已经做完的事", deadline=timezone.now() + timedelta(days=1),
            todo_status=Event.TodoStatus.DONE,
        )
        self._event("已经取消的事", deadline=timezone.now() + timedelta(days=1), is_cancelled=True)
        response = self.client.get(reverse("core:home"))
        self.assertNotContains(response, "已经做完的事")
        self.assertNotContains(response, "已经取消的事")
        self.assertEqual(response.context["stats"]["open_todos"], 0)
        self.assertContains(response, "最近 7 天没有要处理的事")


class ToastRenderingTests(TestCase):
    """轻提示：提示一律飘在右上角，不能占文档流把页面内容挤下去。"""

    def setUp(self):
        self.user = make_user("toaster")
        self.client.force_login(self.user)

    def test_flash_message_renders_in_floating_toast_stack(self):
        response = self.client.post(
            reverse("events:bulk_delete"), {"next": reverse("events:list")}, follow=True
        )
        self.assertContains(response, "请先勾选要删除的事项。")
        self.assertContains(response, "data-toast-stack")
        self.assertContains(response, 'class="toast toast-warning"')
        self.assertContains(response, "data-toast-close")
        # 旧的顶部横幅写法已经不存在：提示不再占 main 的第一屏
        self.assertNotContains(response, 'class="messages"')
        self.assertNotContains(response, "msg msg-warning")
        # 提示落在 main 之外（再配合 CSS 的 fixed 定位，页面内容不会被挤下去）
        body = response.content.decode()
        self.assertLess(body.index("data-toast-stack"), body.index("<main"))
        self.assertNotIn("请先勾选要删除的事项。", body[body.index("<main"):])

    def test_home_draft_count_is_a_dismissible_toast(self):
        batch, _messages, _warnings = create_batch(self.user, RAW)
        process_batch(batch)
        response = self.client.get(reverse("core:home"))
        self.assertContains(response, "条识别结果待确认")
        self.assertContains(response, "data-toast-sticky")


class MarkEvidenceTests(SimpleTestCase):
    """原文就地证据标注：高亮片段 + 字段名标签，替掉独立的「识别依据」栏。"""

    def test_marks_fragment_with_field_label(self):
        html = mark_evidence("请于10月20日前完成报名", [{"start": 2, "end": 8, "label": "截止时间"}])
        self.assertEqual(
            html,
            '请于<mark>10月20日</mark><span class="ev-label">截止时间</span>前完成报名',
        )

    def test_finer_span_wins_over_coarse_overlap(self):
        """规则抽取器把整行当「要求/材料」的证据，粗跨度必须让位给更精确的片段。"""
        html = mark_evidence(
            "请于10月20日前完成报名，报名链接：https://example.edu/contest",
            [
                {"start": 0, "end": 46, "label": "要求/材料"},
                {"start": 2, "end": 8, "label": "截止时间"},
            ],
        )
        self.assertEqual(html.count("<mark>"), 1)
        self.assertIn('<mark>10月20日</mark><span class="ev-label">截止时间</span>', html)
        self.assertNotIn("要求/材料", html)

    def test_non_overlapping_spans_all_kept(self):
        html = mark_evidence(
            "地点：实验楼B302",
            [{"start": 3, "end": 10, "label": "地点"}],
        )
        self.assertEqual(html, '地点：<mark>实验楼B302</mark><span class="ev-label">地点</span>')

    def test_text_is_escaped(self):
        html = mark_evidence("<b>10月20日</b>", [{"start": 3, "end": 9, "label": "截止时间"}])
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;b&gt;", html)

    def test_no_annotations_returns_plain_text(self):
        self.assertEqual(mark_evidence("一段原文", []), "一段原文")
        self.assertEqual(mark_evidence("", [{"start": 0, "end": 1, "label": "x"}]), "")
