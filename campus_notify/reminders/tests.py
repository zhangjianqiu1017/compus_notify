"""提醒单测：默认排期、变更重排、离线补偿、关闭与自定义、邮件送达。"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Event
from events.services import complete_todo, confirm_event, set_fields_from_form
from extraction.pipeline import process_batch
from ingest.services import create_batch
from reminders.models import InAppNotification, Reminder
from reminders.services import dispatch_due, set_reminder_offsets, sync_reminders, toggle_reminder
from tests_helpers import aware, make_user


class ReminderTests(TestCase):
    def setUp(self):
        self.user = make_user("reminder")
        self.client.force_login(self.user)

    def _event(self, deadline_text="请于10月20日前完成报名") -> Event:
        batch, _messages, _warnings = create_batch(self.user, f"【程序设计竞赛报名】\n{deadline_text}")
        result = process_batch(batch)
        return result.drafts[0]

    def test_default_offsets_created_on_confirm(self):
        event = self._event()
        confirm_event(event)  # offsets=None → 使用默认模板
        offsets = sorted(item.offset_minutes for item in event.reminders.all())
        self.assertEqual(offsets, [120, 1440])
        reminder = event.reminders.get(offset_minutes=1440)
        self.assertEqual(reminder.remind_at, aware(2026, 10, 19, 23, 59))
        self.assertEqual(reminder.anchor_kind, Reminder.Anchor.DEADLINE)

    def test_no_reminder_when_anchor_missing(self):
        batch, _messages, _warnings = create_batch(self.user, "【活动通知】地点另行通知，请关注后续消息")
        result = process_batch(batch)
        if not result.drafts:
            self.skipTest("该通知未生成草稿")
        event = result.drafts[0]
        confirm_event(event)
        self.assertEqual(event.reminders.count(), 0)

    def test_dispatch_fires_and_creates_notification(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))
        self.assertEqual(fired, 1)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.SENT)
        notification = InAppNotification.objects.filter(owner=self.user).first()
        self.assertIsNotNone(notification)
        self.assertIn("程序设计竞赛报名", notification.title)

    def test_dispatch_is_idempotent(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        moment = reminder.remind_at + timedelta(seconds=1)
        self.assertEqual(dispatch_due(user=self.user, now=moment), 1)
        self.assertEqual(dispatch_due(user=self.user, now=moment + timedelta(minutes=5)), 0)

    def test_overdue_reminder_is_compensated(self):
        """离线期间到点的提醒，下次打开页面时补偿派发（并标记为已过期）。"""
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(days=1, minutes=1))
        self.assertEqual(fired, 1)
        notification = InAppNotification.objects.get(owner=self.user)
        self.assertEqual(notification.level, InAppNotification.Level.OVERDUE)
        self.assertIn("已过时间", notification.body)

    def test_imminent_reminder_is_marked_warn(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[120])
        reminder = event.reminders.first()
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))
        self.assertEqual(fired, 1)
        notification = InAppNotification.objects.get(owner=self.user)
        self.assertEqual(notification.level, InAppNotification.Level.WARN)
        self.assertIn("即将到期", notification.body)

    def test_too_old_reminder_is_dropped_not_spammed(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(days=10))
        self.assertEqual(fired, 0)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.CANCELLED)

    def test_time_change_reschedules_sent_reminder(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.SENT)

        set_fields_from_form(self.user, event, {"deadline": "2026-10-30T23:59"}, source="edit")
        sync_reminders(event)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.SCHEDULED)
        self.assertIsNone(reminder.sent_at)
        self.assertEqual(reminder.remind_at, aware(2026, 10, 29, 23, 59))

    def test_completed_todo_disables_reminders(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        complete_todo(event)
        self.assertEqual(event.reminders.filter(status=Reminder.Status.SCHEDULED).count(), 0)
        fired = dispatch_due(user=self.user, now=timezone.now() + timedelta(days=1))
        self.assertEqual(fired, 0)

    def test_custom_offsets_replace_defaults(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440, 120])
        set_reminder_offsets(event, [60])
        offsets = [item.offset_minutes for item in event.reminders.all()]
        self.assertEqual(offsets, [60])

    def test_toggle_reminder(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        toggle_reminder(reminder, False)
        reminder.refresh_from_db()
        self.assertFalse(reminder.is_enabled)
        self.assertEqual(dispatch_due(user=self.user, now=reminder.remind_at + timedelta(minutes=1)), 0)
        toggle_reminder(reminder, True)
        reminder.refresh_from_db()
        self.assertTrue(reminder.is_enabled)

    def test_view_round_trip_on_event_detail_page(self):
        """提醒不再有独立页面：详情页里能加、能改、能关、能删，且状态一眼可见。"""
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()

        detail = self.client.get(reverse("events:detail", args=[event.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "截止前 1 天")
        self.assertContains(detail, "添加提醒")

        # 改：1 天 → 2 小时
        self.client.post(
            reverse("reminders:edit", args=[reminder.pk]), {"amount": "2", "unit": "60"}
        )
        reminder.refresh_from_db()
        self.assertEqual(reminder.offset_minutes, 120)
        self.assertContains(
            self.client.get(reverse("events:detail", args=[event.pk])), "截止前 2 小时"
        )

        # 关 → 详情页标「已关闭」
        self.client.post(reverse("reminders:toggle", args=[reminder.pk]), {"enabled": "0"})
        self.assertContains(
            self.client.get(reverse("events:detail", args=[event.pk])), "已关闭"
        )

        # 删 → 回到「还没有提醒」的空态
        self.client.post(reverse("reminders:delete", args=[reminder.pk]))
        self.assertFalse(event.reminders.exists())
        self.assertContains(
            self.client.get(reverse("events:detail", args=[event.pk])), "还没有提醒"
        )

    def test_event_detail_dispatches_due_reminders(self):
        """打开事项详情页即派发到点提醒（离线恢复的补偿也走这里），并显示为已送达。"""
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        # 把触发时间挪到刚刚过去，模拟离线期间错过、回来还能补上的那条
        reminder.remind_at = timezone.now() - timedelta(minutes=5)
        reminder.save(update_fields=["remind_at"])

        response = self.client.get(reverse("events:detail", args=[event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已送达")
        self.assertEqual(
            InAppNotification.objects.filter(owner=self.user, event=event).count(), 1
        )
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.SENT)

    def test_add_reminder_via_view(self):
        """「添加提醒」：一次加一条，不影响已有的。"""
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        response = self.client.post(
            reverse("reminders:add", args=[event.pk]), {"amount": "2", "unit": "60"}
        )
        self.assertEqual(response.status_code, 302)
        offsets = sorted(item.offset_minutes for item in event.reminders.all())
        self.assertEqual(offsets, [120, 1440])

    def test_add_duplicate_reminder_is_rejected(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        response = self.client.post(
            reverse("reminders:add", args=[event.pk]), {"amount": "1", "unit": "1440"}, follow=True
        )
        self.assertContains(response, "已经有一条")
        self.assertEqual(event.reminders.count(), 1)

    def test_add_reminder_rejects_invalid_input(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        response = self.client.post(
            reverse("reminders:add", args=[event.pk]), {"amount": "999", "unit": "1440"}, follow=True
        )
        self.assertContains(response, "有效的提前时间")
        self.assertEqual(event.reminders.count(), 1)

    def test_edit_reminder_via_view(self):
        """修改某一条：只动那一条。"""
        event = self._event()
        confirm_event(event, reminder_offsets=[1440, 120])
        reminder = event.reminders.get(offset_minutes=1440)
        response = self.client.post(
            reverse("reminders:edit", args=[reminder.pk]), {"amount": "3", "unit": "60"}
        )
        self.assertEqual(response.status_code, 302)
        reminder.refresh_from_db()
        self.assertEqual(reminder.offset_minutes, 180)
        self.assertEqual(reminder.label, "截止前 3 小时")
        # 另一条不受影响
        self.assertTrue(event.reminders.filter(offset_minutes=120).exists())

    def test_edit_reminder_reschedules_after_sent(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.SENT)

        self.client.post(reverse("reminders:edit", args=[reminder.pk]), {"amount": "1", "unit": "60"})
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.SCHEDULED)
        self.assertIsNone(reminder.sent_at)
        self.assertEqual(reminder.offset_minutes, 60)

    def test_edit_to_existing_offset_is_rejected(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[1440, 120])
        reminder = event.reminders.get(offset_minutes=1440)
        response = self.client.post(
            reverse("reminders:edit", args=[reminder.pk]), {"amount": "2", "unit": "60"}, follow=True
        )
        self.assertContains(response, "已经存在")
        reminder.refresh_from_db()
        self.assertEqual(reminder.offset_minutes, 1440)

    def test_add_reminder_without_anchor_fails_gracefully(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[])
        event.deadline = None
        event.save(update_fields=["deadline"])
        response = self.client.post(
            reverse("reminders:add", args=[event.pk]), {"amount": "1", "unit": "1440"}, follow=True
        )
        self.assertContains(response, "还没有截止或开始时间")
        self.assertEqual(event.reminders.count(), 0)

    def test_edit_page_keeps_custom_offset_choice(self):
        """自定义提前量必须在编辑页有对应选项，否则保存时会被清掉。"""
        event = self._event()
        confirm_event(event, reminder_offsets=[1440])
        reminder = event.reminders.first()
        self.client.post(reverse("reminders:edit", args=[reminder.pk]), {"amount": "3", "unit": "60"})

        response = self.client.get(reverse("events:edit", args=[event.pk]))
        self.assertContains(response, 'value="180"')

        prefix = f"e{event.pk}"
        data = {
            f"{prefix}-title": event.title,
            f"{prefix}-category": event.category,
            f"{prefix}-course": "",
            f"{prefix}-location": "",
            f"{prefix}-organizer": "",
            f"{prefix}-link": "",
            f"{prefix}-contact": "",
            f"{prefix}-requirement": "",
            f"{prefix}-deadline": "2026-10-20T23:59",
            f"{prefix}-start_at": "",
            f"{prefix}-end_at": "",
            f"{prefix}-reminder_offsets": ["180"],
        }
        self.client.post(reverse("events:edit", args=[event.pk]), data)
        self.assertEqual(
            sorted(item.offset_minutes for item in event.reminders.all() if item.status != "cancelled"),
            [180],
        )

    def test_reminder_units_split(self):
        from reminders.services import offset_from_parts, offset_parts

        self.assertEqual(offset_parts(1440), (1, 1440))
        self.assertEqual(offset_parts(120), (2, 60))
        self.assertEqual(offset_parts(45), (45, 1))
        self.assertEqual(offset_from_parts("2", "60"), 120)
        self.assertIsNone(offset_from_parts("-1", "60"))
        self.assertIsNone(offset_from_parts("abc", "60"))

    def test_api_notifications_endpoint(self):
        event = self._event()
        confirm_event(event, reminder_offsets=[0])
        reminder = event.reminders.first()
        reminder.remind_at = timezone.now() - timedelta(minutes=1)
        reminder.save(update_fields=["remind_at"])
        response = self.client.get(reverse("api_notifications"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["unread"], 1)
        self.assertEqual(len(payload["items"]), 1)


class ReminderEmailTests(TestCase):
    """FR-08 邮件通道：提醒到点后发信，而不是只在站内留一条通知。

    测试跑在 locmem 邮件后端上（见 settings.py），所以 mail.outbox 就是
    「用户邮箱里收到了什么」。
    """

    def setUp(self):
        self.user = make_user("remindermail")
        self.user.email = "mail@example.edu"
        self.user.save(update_fields=["email"])
        self.client.force_login(self.user)

    def _due_reminder(self, offsets=(1440,)) -> Reminder:
        batch, _messages, _warnings = create_batch(
            self.user, "【程序设计竞赛报名】\n请于10月20日前完成报名"
        )
        event = process_batch(batch).drafts[0]
        confirm_event(event, reminder_offsets=list(offsets))
        return event.reminders.first()

    def test_dispatch_sends_email_to_user(self):
        reminder = self._due_reminder(offsets=[1440])
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))

        self.assertEqual(fired, 1)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["mail@example.edu"])
        # 标题带提醒标签与事件名，收件箱列表里不点开也能看懂
        self.assertIn("程序设计竞赛报名", message.subject)
        self.assertIn("1 天", message.subject)
        # 正文给出时间、要求/材料与可点的详情链接
        self.assertIn("10月20日 23:59", message.body)
        self.assertIn("要求：请于10月20日前完成报名", message.body)
        self.assertIn("/events/", message.body)
        self.assertIn("/accounts/profile/", message.body)  # 一键退订入口

        notification = InAppNotification.objects.get(owner=self.user)
        self.assertIsNotNone(notification.email_sent_at)
        self.assertEqual(getattr(dispatch_due, "last_emails_sent"), 1)

    def test_email_sent_once_per_reminder(self):
        """派发幂等：同一条提醒不会被重复发信。"""
        reminder = self._due_reminder()
        moment = reminder.remind_at + timedelta(seconds=1)
        dispatch_due(user=self.user, now=moment)
        dispatch_due(user=self.user, now=moment + timedelta(minutes=5))
        self.assertEqual(len(mail.outbox), 1)

    def test_overdue_compensation_email_says_overdue(self):
        """离线补发的提醒，邮件里要写明已经过时间，而不是假装还来得及。"""
        reminder = self._due_reminder()
        # 错过到点一天多：仍在补偿窗口内，但事项已经过期
        dispatch_due(user=self.user, now=reminder.remind_at + timedelta(days=1, minutes=1))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("已过期", message.subject)
        self.assertIn("已过时间", message.body)

    def test_preference_off_still_notifies_in_app(self):
        """关掉邮件提醒只是不发信：站内通知必须照旧，否则用户会彻底漏掉提醒。"""
        from accounts.models import UserPreference

        preference = UserPreference.for_user(self.user)
        preference.reminder_email_enabled = False
        preference.save(update_fields=["reminder_email_enabled"])

        reminder = self._due_reminder()
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))

        self.assertEqual(fired, 1)
        self.assertEqual(mail.outbox, [])
        notification = InAppNotification.objects.get(owner=self.user)
        self.assertIsNone(notification.email_sent_at)

    @override_settings(EMAIL_REMINDER_ENABLED=False)
    def test_global_switch_off_skips_email_only(self):
        reminder = self._due_reminder()
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))
        self.assertEqual(fired, 1)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(InAppNotification.objects.filter(owner=self.user).count(), 1)

    def test_no_email_address_is_not_a_crash(self):
        """历史账号可能没有邮箱：不报错、不发信，站内提醒照常。"""
        from accounts.models import UserPreference

        self.user.email = ""
        self.user.username = "noemail"
        self.user.save(update_fields=["email", "username"])
        UserPreference.for_user(self.user)

        reminder = self._due_reminder()
        fired = dispatch_due(user=self.user, now=reminder.remind_at + timedelta(seconds=1))
        self.assertEqual(fired, 1)
        self.assertEqual(mail.outbox, [])
        notification = InAppNotification.objects.get(owner=self.user)
        self.assertIsNone(notification.email_sent_at)

    def test_send_failure_is_retried_not_lost(self):
        """SMTP 抖一下不能把提醒吞掉：留痕为空 → 下次派发重试。"""
        reminder = self._due_reminder()
        moment = reminder.remind_at + timedelta(seconds=1)

        with patch("reminders.services.send_mail", side_effect=OSError("SMTP 挂了")):
            fired = dispatch_due(user=self.user, now=moment)
        self.assertEqual(fired, 1)  # 站内通知照常派发
        self.assertEqual(mail.outbox, [])
        notification = InAppNotification.objects.get(owner=self.user)
        self.assertIsNone(notification.email_sent_at)
        # 提醒已标记送达，不会被派发第二次
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.SENT)

        # 邮件在下一次派发时补上
        from reminders.services import send_reminder_email

        self.assertTrue(send_reminder_email(notification))
        self.assertEqual(len(mail.outbox), 1)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.email_sent_at)

    @override_settings(EMAIL_REMINDER_MAX_PER_DISPATCH=1)
    def test_email_flood_is_capped_per_dispatch(self):
        """离线多日再上线：一次只发上限封邮件，剩下的留到下次，不是丢弃。"""
        batch, _messages, _warnings = create_batch(
            self.user, "【程序设计竞赛报名】\n请于10月20日前完成报名"
        )
        event = process_batch(batch).drafts[0]
        confirm_event(
            event, reminder_offsets=[1440, 120, 60, 0]
        )
        # 四条提醒全部到点：默认「提前量落到现在」，所以 now 要晚于最晚的那条
        now = max(item.remind_at for item in event.reminders.all()) + timedelta(seconds=1)

        self.assertEqual(dispatch_due(user=self.user, now=now), 4)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(getattr(dispatch_due, "last_emails_sent"), 1)
        self.assertEqual(
            InAppNotification.objects.filter(owner=self.user, email_sent_at__isnull=True).count(), 3
        )

    def test_api_polling_also_sends_email(self):
        """用户没配计划任务时，页面轮询就是邮件发出点。"""
        reminder = self._due_reminder()
        reminder.remind_at = timezone.now() - timedelta(minutes=1)
        reminder.save(update_fields=["remind_at"])

        response = self.client.get(reverse("api_notifications"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unread"], 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_management_command_reports_emails(self):
        from io import StringIO

        from django.core.management import call_command

        reminder = self._due_reminder()
        reminder.remind_at = timezone.now() - timedelta(minutes=1)
        reminder.save(update_fields=["remind_at"])

        out = StringIO()
        call_command("dispatch_reminders", stdout=out)
        self.assertIn("邮件 1 封", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)

    def test_profile_page_shows_recipient_address(self):
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, "mail@example.edu")
        self.assertContains(response, "提醒同时发到我的邮箱")

