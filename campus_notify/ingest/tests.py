"""导入侧单测：消息切分、敏感扫描、幂等、原文丢弃。"""
from __future__ import annotations

from django.test import TestCase

from ingest.models import Batch, Message
from ingest.services import (
    DuplicateImport,
    create_batch,
    discard_raw_if_needed,
    message_hash,
    normalize_text,
    purge_raw_texts,
    scan_sensitive,
    split_messages,
)
from tests_helpers import make_user


def create_event_from_message(user, batch, message):
    """让原文真正挂到一个事项上（模拟一次完整的导入 + 识别）。

    去重拦截的前提是「原文还关联着事项」，所以测试必须先建立这个关联。
    """
    from events.models import Event, EventMessage

    event = Event.objects.create(owner=user, title=message.raw_text[:40], batch=batch)
    EventMessage.objects.create(event=event, message=message, relation="primary")
    return event


class SplitMessagesTests(TestCase):
    def test_split_by_sender_and_timestamp_headers(self):
        raw = (
            "张三 2026-10-08 09:12\n"
            "【程序设计竞赛报名】\n"
            "请于10月20日前完成报名\n"
            "张三 2026-10-08 09:15\n"
            "补充：报名地点改到实验楼B302"
        )
        messages, confident = split_messages(raw, source_label="软件2201班群")
        self.assertTrue(confident)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].sender, "张三")
        self.assertIsNotNone(messages[0].sent_at)
        self.assertEqual(messages[0].sent_at.hour, 9)
        self.assertIn("补给", messages[1].raw_text.replace("补充", "补给"))

    def test_inline_sender_prefix_is_boundary(self):
        messages, confident = split_messages("李四：明天下午三点在报告厅开会\n王五：收到")
        self.assertTrue(confident)
        self.assertEqual([item.sender for item in messages], ["李四", "王五"])

    def test_field_labels_are_not_treated_as_senders(self):
        raw = "注意：请携带学生证\n地点：实验楼B302\n时间：10月20日 14:00"
        messages, confident = split_messages(raw)
        self.assertFalse(confident)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender, "")

    def test_uncertain_boundary_falls_back_to_single_message(self):
        raw = "这是一段没有任何发送者标记的连续通知内容，包含截止时间10月20日，请准时提交。"
        messages, confident = split_messages(raw)
        self.assertFalse(confident)
        self.assertEqual(len(messages), 1)

    def test_blank_lines_are_not_message_boundaries(self):
        """回归：用户粘的一段通知就是一条原文，空行只是排版，不能拆成「原始 + 补充」。"""
        raw = (
            "【程序设计竞赛报名】\n"
            "请于10月20日前完成报名，报名链接见群公告。\n"
            "\n"
            "另外：报名地点改到实验楼B302，请携带学生证。"
        )
        messages, confident = split_messages(raw)
        self.assertFalse(confident)
        self.assertEqual(len(messages), 1)
        self.assertIn("请于10月20日前完成报名", messages[0].raw_text)
        self.assertIn("实验楼B302", messages[0].raw_text)

    def test_normalize_text_drops_zero_width(self):
        self.assertEqual(normalize_text("a\u200bb\r\nc"), "ab\nc")


class SensitiveScanTests(TestCase):
    def test_detect_patterns(self):
        text = "学号：2023010101 手机号 13812345678 邮箱 a@b.com"
        kinds = scan_sensitive(text)
        self.assertIn("学号", kinds)
        self.assertIn("手机号", kinds)
        self.assertIn("邮箱", kinds)

    def test_clean_text(self):
        self.assertEqual(scan_sensitive("明天下午开会"), [])


class CreateBatchTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")

    def test_batch_and_messages_created(self):
        batch, messages, warnings = create_batch(self.user, "张三 2026-10-08 09:12\n10月20日前提交作业")
        self.assertEqual(batch.owner, self.user)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].seq, 1)
        self.assertEqual(warnings, [])

    def test_multiparagraph_paste_stores_one_message(self):
        """一段带空行的粘贴只落一条原文，不会拆成 primary + supplement 两条。

        没有发送者/时间戳边界是正常的粘贴方式，不该因此警告用户。
        """
        raw = "【通知】请于10月20日前提交作业\n\n补充：提交地点改为实验楼B302"
        _batch, created, warnings = create_batch(self.user, raw)
        self.assertEqual(len(created), 1)
        self.assertEqual(Message.objects.filter(owner=self.user).count(), 1)
        self.assertIn("实验楼B302", created[0].raw_text)
        self.assertEqual(warnings, [])

    def test_duplicate_import_is_idempotent(self):
        """同一段原文只存一份：重复导入不会新建第二条原文。"""
        raw = "【通知】请于10月20日前完成报名"
        _batch, first, _ = create_batch(self.user, raw)
        _batch2, second, warnings = create_batch(self.user, raw)
        self.assertEqual(Message.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(first[0].pk, second[0].pk)
        self.assertTrue(any("已有原文" in warning for warning in warnings))

    def test_duplicate_import_of_linked_message_carries_details(self):
        """原文还挂在事项上时 → 拦截并说明它关联了什么。"""
        raw = "【通知】请于10月20日前完成报名"
        batch, messages, _ = create_batch(self.user, raw)
        event = create_event_from_message(self.user, batch, messages[0])

        with self.assertRaises(DuplicateImport) as ctx:
            create_batch(self.user, raw)
        self.assertEqual(len(ctx.exception.duplicates), 1)
        detail = ctx.exception.duplicates[0]
        self.assertIn("10月20日", detail["preview"])
        self.assertEqual([item.pk for item in detail["events"]], [event.pk])
        self.assertIn("仍关联着", str(ctx.exception))

    def test_reimport_after_deleting_event_is_transparent(self):
        """回归：识别结果被删掉后重新导入，不该再拦人。"""
        raw = "【心理健康部通知】\n时间：2026年10月10日 15:00-18:00\n地点：北校体育馆前停车场"
        batch, messages, _ = create_batch(self.user, raw)
        event = create_event_from_message(self.user, batch, messages[0])
        event.delete()

        batch2, reused, warnings = create_batch(self.user, raw)
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0].pk, messages[0].pk)
        self.assertEqual(Message.objects.filter(owner=self.user).count(), 1)
        self.assertTrue(any("没有对应事项" in warning for warning in warnings))

    def test_duplicate_import_can_be_forced_to_reuse(self):
        """「仍然重新识别」：复用已有原文，不新建副本。"""
        raw = "【通知】请于10月20日前完成报名"
        first_batch, first_messages, _ = create_batch(self.user, raw)
        create_event_from_message(self.user, first_batch, first_messages[0])

        batch, messages, warnings = create_batch(self.user, raw, reuse_existing=True)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].pk, first_messages[0].pk)
        self.assertEqual(messages[0].batch_id, batch.pk)
        self.assertEqual(Message.objects.filter(owner=self.user).count(), 1)
        self.assertTrue(any("复用" in warning for warning in warnings))
        # 源批次已被搬空，不留空壳
        self.assertFalse(Batch.objects.filter(pk=first_batch.pk).exists())

    def test_forced_reuse_restores_discarded_raw_text(self):
        raw = "【通知】请于10月20日前完成报名"
        _batch, messages, _ = create_batch(self.user, raw)
        messages[0].raw_text = "[原文已按隐私设置丢弃]"
        messages[0].raw_discarded = True
        messages[0].save(update_fields=["raw_text", "raw_discarded"])

        _batch2, reused, _ = create_batch(self.user, raw, reuse_existing=True)
        reused[0].refresh_from_db()
        self.assertFalse(reused[0].raw_discarded)
        self.assertEqual(reused[0].raw_text, raw)

    def test_partial_duplicate_keeps_new_messages(self):
        """「部分重复」现在只可能出现在有真实边界的批次里（群聊记录）：撞的那条跳过，新的保留。"""
        full = (
            "张三 2026-10-08 09:12\n【通知A】请于10月20日前完成报名\n"
            "李四 2026-10-08 09:20\n【通知B】请于11月1日前提交材料"
        )
        batch, messages, _ = create_batch(self.user, "张三 2026-10-08 09:12\n【通知A】请于10月20日前完成报名")
        create_event_from_message(self.user, batch, messages[0])

        batch2, created, warnings = create_batch(self.user, full)
        self.assertEqual(len(created), 1)
        self.assertIn("通知B", created[0].raw_text)
        self.assertTrue(any("跳过" in warning for warning in warnings))

    def test_same_hash_different_user_is_allowed(self):
        raw = "【通知】请于10月20日前完成报名"
        other = make_user("bob")
        create_batch(self.user, raw)
        create_batch(other, raw)
        self.assertEqual(Message.objects.count(), 2)

    def test_message_hash_stable(self):
        self.assertEqual(message_hash("abc"), message_hash(" abc "))


class PrivacyTests(TestCase):
    def setUp(self):
        self.user = make_user("carol")

    def test_discard_raw_respects_preference(self):
        from accounts.models import UserPreference

        preference = UserPreference.for_user(self.user)
        preference.store_raw_messages = False
        preference.save()
        _, messages, _ = create_batch(self.user, "【通知】10月20日前提交实验报告")
        discarded = discard_raw_if_needed(self.user, messages)
        self.assertEqual(discarded, 1)
        messages[0].refresh_from_db()
        self.assertTrue(messages[0].raw_discarded)
        self.assertEqual(messages[0].display_text(), "[原文已按隐私设置丢弃]")

    def test_purge_raw_keeps_structured_data(self):
        _, messages, _ = create_batch(self.user, "【通知】10月20日前提交实验报告")
        count = purge_raw_texts(self.user)
        self.assertEqual(count, 1)
        self.assertTrue(Message.objects.get(pk=messages[0].pk).raw_discarded)
