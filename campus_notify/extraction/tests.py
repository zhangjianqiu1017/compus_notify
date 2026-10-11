"""识别链路单测：时间归一、规则抽取、归并打分、冲突消解、端到端流水线。"""
from __future__ import annotations

import os
import unittest
from datetime import date, timedelta

from django.test import TestCase, override_settings

from events.models import Event
from events.services import confirm_event
from extraction.fields import (
    CATEGORY_HOMEWORK,
    FIELD_COURSE,
    FIELD_DEADLINE,
    FIELD_LOCATION,
    FIELD_REQUIREMENT,
    FIELD_START_AT,
    RELATION_CORRECTION,
    RELATION_PRIMARY,
)
from extraction.merge import (
    DECISION_MERGE,
    DECISION_NEW,
    DECISION_REVIEW,
    EventSnapshot,
    anchor_kind_differs,
    anchors_conflict,
    decide,
    normalize_title,
    score_pair,
)
from extraction.normalize import extract_temporal, resolve_datetime
from extraction.pipeline import process_batch
from extraction.providers.base import (
    ExtractedEvent,
    ExtractionOutcome,
    ExtractionProvider,
    FieldValue,
    MessageRelation,
    is_noise,
)
from extraction.resolve import IncomingField, merge_multi_value, should_replace
from ingest.services import create_batch
from tests_helpers import aware, make_user

# 一条通知里的两个阶段：先提交材料（截止），后课堂汇报（发生）。
# 用户粘的是一整段（只有开头一处发送者边界）→ 一条原文，两个阶段由抽取环节在同一段里分辨。
MILESTONE_NOTICE = (
    "张老师 2026-10-10 09:00\n"
    "软件设计小组作业提交通知\n"
    "各组请于【10月15日 18:00】前提交： ① 汇报PPT（命名：第X组+组长姓名） "
    "② 小组分工说明（含每人贡献占比）\n"
    "课堂汇报时间：【10月17日】，每组【8分钟】汇报+【2分钟】提问。请提前调试设备，控制好时间。"
)


class NormalizeTests(TestCase):
    """相对时间必须以「消息发送时间」为锚点，而不是「当前时间」。"""

    def test_relative_time_anchored_to_message_time(self):
        anchor = aware(2026, 10, 8, 9, 0)
        self.assertEqual(resolve_datetime("明天下午3点交", anchor), aware(2026, 10, 9, 15, 0))
        self.assertEqual(resolve_datetime("今晚8点开会", anchor), aware(2026, 10, 8, 20, 0))
        self.assertEqual(resolve_datetime("三天后提交", anchor), resolve_datetime("3天后提交", anchor))
        self.assertEqual(resolve_datetime("3天后提交", anchor), aware(2026, 10, 11, 0, 0))

    def test_relative_time_differs_for_different_message_days(self):
        older = resolve_datetime("明天中午截止", aware(2026, 10, 1, 10, 0))
        newer = resolve_datetime("明天中午截止", aware(2026, 10, 20, 10, 0))
        self.assertEqual(older.date(), date(2026, 10, 2))
        self.assertEqual(newer.date(), date(2026, 10, 21))

    def test_month_day_infers_next_year(self):
        anchor = aware(2026, 12, 20, 9, 0)
        self.assertEqual(resolve_datetime("1月5日交材料", anchor).date(), date(2027, 1, 5))

    def test_deadline_default_time_is_2359(self):
        anchor = aware(2026, 10, 8, 9, 0)
        resolved = resolve_datetime("请于10月20日前完成报名", anchor, field=FIELD_DEADLINE)
        self.assertEqual(resolved, aware(2026, 10, 20, 23, 59))

    def test_explicit_time_is_kept(self):
        anchor = aware(2026, 10, 8, 9, 0)
        self.assertEqual(resolve_datetime("10月20日 14:30 开始", anchor), aware(2026, 10, 20, 14, 30))

    def test_week_prefix(self):
        anchor = aware(2026, 10, 8, 9, 0)  # 2026-10-08 是周四
        self.assertEqual(resolve_datetime("下周三下午2点面试", anchor).date(), date(2026, 10, 14))
        self.assertEqual(resolve_datetime("本周五提交", anchor).date(), date(2026, 10, 9))
        self.assertEqual(resolve_datetime("周三前提交", anchor).date(), date(2026, 10, 14))

    @override_settings(TERM_START_DATE="2026-09-07")
    def test_term_week(self):
        anchor = aware(2026, 10, 8, 9, 0)
        self.assertEqual(resolve_datetime("第7周周三交实验报告", anchor).date(), date(2026, 10, 21))
        self.assertEqual(resolve_datetime("第7周交实验报告", anchor).date(), date(2026, 10, 19))

    def test_time_range_produces_start_and_end(self):
        hits = extract_temporal("10月20日 14:00-16:00 举行宣讲会", aware(2026, 10, 8, 9, 0))
        kinds = {hit.kind_hint for hit in hits}
        self.assertIn("range_start", kinds)
        self.assertIn("range_end", kinds)
        starts = [hit for hit in hits if hit.kind_hint == "range_start"]
        self.assertEqual(starts[0].dt, aware(2026, 10, 20, 14, 0))

    def test_only_time_rolls_to_next_day_if_passed(self):
        anchor = aware(2026, 10, 8, 18, 0)
        self.assertEqual(resolve_datetime("上午9点交", anchor).date(), date(2026, 10, 9))

    def test_hour_offset_is_exact(self):
        anchor = aware(2026, 10, 8, 9, 0)
        self.assertEqual(resolve_datetime("2小时后集合", anchor), aware(2026, 10, 8, 11, 0))


class NoiseTests(TestCase):
    def test_chitchat_is_noise(self):
        for text in ("收到", "好的", "哈哈哈", "谢谢老师", "嗯嗯"):
            self.assertTrue(is_noise(text), text)

    def test_real_notice_is_not_noise(self):
        self.assertFalse(is_noise("请于10月20日前完成报名"))


class MergeScoreTests(TestCase):
    def test_identical_url_is_hard_match(self):
        left = EventSnapshot(title="程序设计竞赛报名", url="https://a.edu/x")
        right = EventSnapshot(title="另外一个完全不同的标题", url="https://a.edu/x")
        score, reasons = score_pair(left, right)
        self.assertGreaterEqual(score, 0.99)
        self.assertEqual(decide(score), DECISION_MERGE)

    def test_identical_title_only_stays_in_review_zone(self):
        left = EventSnapshot(title="高等数学作业", deadline=aware(2026, 10, 20, 23, 59))
        right = EventSnapshot(title="【高等数学作业】通知")
        score, _ = score_pair(left, right)
        self.assertEqual(decide(score), DECISION_REVIEW)

    def test_title_and_time_together_auto_merge(self):
        left = EventSnapshot(
            title="程序设计竞赛报名", course="软件工程",
            deadline=aware(2026, 10, 20, 23, 59),
        )
        right = EventSnapshot(
            title="关于程序设计竞赛报名的通知", course="软件工程",
            deadline=aware(2026, 10, 20, 23, 59),
        )
        score, _ = score_pair(left, right)
        self.assertEqual(decide(score), DECISION_MERGE)

    def test_unrelated_events_are_new(self):
        left = EventSnapshot(title="高等数学期末考试", deadline=aware(2026, 10, 20))
        right = EventSnapshot(title="校园歌手大赛报名", deadline=aware(2026, 11, 30))
        score, _ = score_pair(left, right)
        self.assertEqual(decide(score), DECISION_NEW)

    def test_title_normalization(self):
        self.assertEqual(normalize_title("【关于程序设计竞赛报名的通知】"), normalize_title("程序设计竞赛报名"))

    @override_settings(MERGE_AUTO_THRESHOLD=0.5, MERGE_NEW_THRESHOLD=0.2)
    def test_thresholds_are_configurable(self):
        self.assertEqual(decide(0.6), DECISION_MERGE)
        self.assertEqual(decide(0.3), DECISION_REVIEW)

    def test_anchor_rules_separate_milestones(self):
        """截止（提交阶段）与开始（现场阶段）是两件事，不能互相归并。"""
        submit = EventSnapshot(title="软件设计小组作业提交", deadline=aware(2026, 10, 15, 18, 0))
        report = EventSnapshot(title="软件设计小组课堂汇报", start_at=aware(2026, 10, 17, 0, 0))
        self.assertTrue(anchor_kind_differs(submit, report))
        self.assertTrue(anchors_conflict(submit, report))

        # 同一阶段的重复发布（锚点一致）不受这条规则约束，仍走正常打分
        repeat = EventSnapshot(title="软件设计小组作业提交提醒", deadline=aware(2026, 10, 15, 18, 0))
        self.assertFalse(anchors_conflict(submit, repeat))

        # 同角色但取值不同的两个阶段（一条消息里的两个截止）也算冲突
        other = EventSnapshot(title="软件设计小组作业提交", deadline=aware(2026, 10, 20, 23, 59))
        self.assertTrue(anchors_conflict(submit, other))

        # 缺时间的画像不参与这条规则，避免把「还没识别出时间」误判成另一个阶段
        self.assertFalse(anchors_conflict(EventSnapshot(title="待定事项"), submit))


class MilestoneSplitProvider(ExtractionProvider):
    """假 Provider：模拟大模型把一条通知拆成「提交 / 汇报」两个阶段。

    规则抽取器按设计不拆分（见 providers/rules.py 的能力边界说明），
    所以拆分链路用这个假 Provider 固定输入，测试才是确定性的。
    """

    name = "fake-split"
    is_llm = False
    supports_recheck = False

    def __init__(self, *, titles=("软件设计小组作业提交", "软件设计小组课堂汇报")):
        self.titles = titles

    def extract(self, messages, *, batch=None):
        message = messages[0]
        text = message.raw_text

        def field(name, value, *, has_time=True):
            index = text.find(value)
            span = (index, index + len(value)) if index >= 0 else None
            return FieldValue(
                field=name,
                value=value,
                confidence=0.9,
                span=span,
                message_index=message.seq,
                has_time=has_time,
            )

        submit = ExtractedEvent(
            title=self.titles[0],
            category=CATEGORY_HOMEWORK,
            confidence=0.9,
            fields=[
                field(FIELD_DEADLINE, "10月15日 18:00"),
                field(FIELD_COURSE, "软件设计"),
                field(
                    FIELD_REQUIREMENT,
                    "① 汇报PPT（命名：第X组+组长姓名） ② 小组分工说明（含每人贡献占比）",
                ),
            ],
            messages=[MessageRelation(index=message.seq, relation=RELATION_PRIMARY)],
        )
        report = ExtractedEvent(
            title=self.titles[1],
            category=CATEGORY_HOMEWORK,
            confidence=0.9,
            fields=[
                field(FIELD_START_AT, "10月17日", has_time=False),
                field(FIELD_COURSE, "软件设计"),
                field(FIELD_REQUIREMENT, "每组【8分钟】汇报+【2分钟】提问。请提前调试设备"),
            ],
            messages=[MessageRelation(index=message.seq, relation=RELATION_PRIMARY)],
        )
        return ExtractionOutcome(events=[submit, report], provider=self.name)

    def describe(self) -> str:  # pragma: no cover - 仅测试用
        return "假抽取器（固定拆成两个阶段）"


class MilestoneSplitTests(TestCase):
    """一条通知里的多个阶段（先提交、后汇报）必须拆成两个事项。

    提示词负责让大模型拆，这里用假 Provider 固定住拆的结果，
    验证拆完之后不会被归并层再并回一件、也不会在重新识别时串字段。
    """

    def setUp(self):
        self.user = make_user("milestone")

    def _run(self, provider, raw=MILESTONE_NOTICE):
        batch, _messages, _warnings = create_batch(self.user, raw)
        return process_batch(batch, provider=provider)

    def test_notice_splits_into_submit_and_report(self):
        result = self._run(MilestoneSplitProvider())
        self.assertEqual(len(result.merged), 0)
        self.assertEqual(len(result.drafts), 2)

        by_title = {event.title: event for event in result.drafts}
        submit = by_title["软件设计小组作业提交"]
        report = by_title["软件设计小组课堂汇报"]

        self.assertEqual(submit.deadline, aware(2026, 10, 15, 18, 0))
        self.assertIsNone(submit.start_at)
        self.assertIsNone(report.deadline)
        self.assertEqual(report.start_at, aware(2026, 10, 17, 0, 0))

        # 两个交付物共享同一个截止，写在同一条要求里（FR-14-1：不按交付物拆）
        requirement = submit.current_field(FIELD_REQUIREMENT).value_text
        self.assertIn("第X组", requirement)
        self.assertIn("贡献占比", requirement)
        # 汇报时长不是提交阶段的时间点
        self.assertNotIn("8分钟", requirement)

        # 两个事项共享同一条原文，都能回看原文（US-06）
        self.assertEqual(submit.links.count(), 1)
        self.assertEqual(report.links.count(), 1)
        self.assertEqual(submit.links.first().message_id, report.links.first().message_id)

    def test_siblings_do_not_merge_even_with_identical_titles(self):
        """回归：标题再像也不能把两个阶段并成一件。

        并成一件的症状是同一事项同时带 deadline 与 start_at ——
        而提醒编排按「截止优先」只排一套，10月17日的课堂汇报会彻底没有提醒。
        """
        provider = MilestoneSplitProvider(
            titles=("软件设计小组作业提交", "软件设计小组作业提交")
        )
        result = self._run(provider)
        self.assertEqual(len(result.merged), 0)
        self.assertEqual(len(result.drafts), 2)
        for event in result.drafts:
            self.assertIsNone(event.review_candidate, "同批次的两个阶段不应互为重复候选")
            self.assertFalse(event.needs_review, "拆开是正常结果，不需要打扰用户")
            self.assertFalse(event.deadline and event.start_at, "一个事项不应同时带两个锚点")

        self.assertIn(aware(2026, 10, 15, 18, 0), [event.deadline for event in result.drafts])
        self.assertIn(aware(2026, 10, 17, 0, 0), [event.start_at for event in result.drafts])

    def test_reimport_keeps_the_split_and_each_reminder(self):
        """重新识别：两个阶段各自归回自己，不把截止时间灌进汇报事项。"""
        provider = MilestoneSplitProvider()
        result = self._run(provider)
        by_title = {event.title: event for event in result.drafts}
        submit = by_title["软件设计小组作业提交"]
        report = by_title["软件设计小组课堂汇报"]
        confirm_event(submit, reminder_offsets=[1440])
        confirm_event(report, reminder_offsets=[30])
        self.assertEqual(submit.reminders.count(), 1)
        self.assertEqual(report.reminders.count(), 1)

        from ingest.services import create_batch as make_batch

        batch2, _messages, _warnings = make_batch(self.user, MILESTONE_NOTICE, reuse_existing=True)
        result2 = process_batch(batch2, provider=provider)

        self.assertEqual(len(result2.drafts), 0, "同一原文不该再生成重复草稿")
        self.assertEqual(len(result2.merged), 2)
        self.assertEqual(Event.objects.filter(owner=self.user).count(), 2)

        submit.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(submit.deadline, aware(2026, 10, 15, 18, 0))
        self.assertIsNone(submit.start_at)
        self.assertIsNone(report.deadline)
        self.assertEqual(report.start_at, aware(2026, 10, 17, 0, 0))
        # 两个阶段各自的提醒都还在（截止优先的锚点规则下，并成一件必然丢一个）
        self.assertEqual(submit.reminders.count(), 1)
        self.assertEqual(report.reminders.count(), 1)


class ConflictResolutionTests(TestCase):
    def _existing(self, value="10月15日", confidence=0.8, sent=None):
        class FakeField:
            value_text = value
            value_dt = None

        field = FakeField()
        field.confidence = confidence
        field.valid_from = sent
        return field

    def test_correction_replaces(self):
        incoming = IncomingField(field="deadline", value_text="10月25日", is_correction=True)
        replace, reason = should_replace(self._existing(), incoming)
        self.assertTrue(replace)
        self.assertIn("更正", reason)

    def test_later_message_replaces(self):
        incoming = IncomingField(field="deadline", sent_at=aware(2026, 10, 9))
        replace, reason = should_replace(self._existing(sent=aware(2026, 10, 8)), incoming)
        self.assertTrue(replace)
        self.assertIn("更晚", reason)

    def test_earlier_message_does_not_replace(self):
        incoming = IncomingField(field="deadline", sent_at=aware(2026, 10, 7))
        replace, reason = should_replace(self._existing(sent=aware(2026, 10, 8)), incoming)
        self.assertFalse(replace)

    def test_higher_confidence_wins_when_time_unknown(self):
        incoming = IncomingField(field="location", confidence=0.95)
        replace, _ = should_replace(self._existing(confidence=0.6), incoming)
        self.assertTrue(replace)

    def test_multi_value_fields_accumulate(self):
        merged = merge_multi_value("请携带学生证", "请携带学生证；请提前15分钟到场")
        self.assertIn("学生证", merged)
        self.assertIn("提前15分钟", merged)


class RuleProviderTests(TestCase):
    def setUp(self):
        self.user = make_user("rules")

    def test_notice_fields_extracted(self):
        raw = (
            "【程序设计竞赛报名】\n"
            "请于10月20日前完成报名，报名链接：https://example.edu/contest\n"
            "地点：实验楼B302\n"
            "联系人：李老师"
        )
        batch, _messages, _warnings = create_batch(self.user, raw)
        result = process_batch(batch)
        event = result.drafts[0]
        self.assertEqual(event.title, "程序设计竞赛报名")
        self.assertEqual(event.deadline, aware(2026, 10, 20, 23, 59))
        self.assertEqual(event.location, "实验楼B302")
        self.assertEqual(event.link, "https://example.edu/contest")
        self.assertEqual(event.current_field("contact").value_text, "李老师")


class PipelineTests(TestCase):
    def setUp(self):
        self.user = make_user("pipeline")

    def _run(self, raw, **kwargs):
        batch, messages, _warnings = create_batch(self.user, raw, **kwargs)
        return batch, process_batch(batch)

    def test_every_field_has_evidence_span(self):
        raw = "张三 2026-10-08 09:12\n【程序设计竞赛报名】\n请于10月20日前完成报名"
        _batch, result = self._run(raw)
        event = result.drafts[0]
        field = event.current_field(FIELD_DEADLINE)
        self.assertEqual(field.evidence_text, "10月20日")
        self.assertIsNotNone(field.source_message)

    def test_missing_time_is_flagged_for_review(self):
        _batch, result = self._run("请同学们积极参加本次活动，地点另行通知")
        self.assertTrue(result.drafts or result.processed)
        if result.drafts:
            self.assertTrue(result.drafts[0].needs_review)

    def test_supplement_in_same_batch_merges_into_one_event(self):
        raw = (
            "张三 2026-10-08 09:12\n"
            "【程序设计竞赛报名】\n"
            "请于10月20日前完成报名\n"
            "张三 2026-10-08 09:15\n"
            "更正：报名地点改到实验楼B302"
        )
        _batch, result = self._run(raw)
        # 两条消息归并成同一件事，且只剩一个待人确认草稿
        self.assertEqual(len(result.merged), 1)
        self.assertEqual(len(result.drafts), 1)
        event = result.merged[0].event
        self.assertEqual(event.pk, result.drafts[0].pk)
        self.assertEqual(event.links.count(), 2)
        self.assertEqual(event.location, "实验楼B302")
        relations = sorted(link.relation for link in event.links.all())
        self.assertIn(RELATION_CORRECTION, relations)

    def test_correction_across_batches_updates_value_and_keeps_history(self):
        batch1, result1 = self._run(
            "【程序设计竞赛报名】\n请于10月20日前完成报名，报名链接：https://example.edu/contest"
        )
        event = result1.drafts[0]
        confirm_event(event, reminder_offsets=[1440])
        event.refresh_from_db()
        self.assertEqual(event.deadline, aware(2026, 10, 20, 23, 59))
        self.assertEqual(event.reminders.count(), 1)
        old_remind_at = event.reminders.first().remind_at

        _batch2, result2 = self._run("更正：报名截止时间改为10月25日")
        self.assertEqual(len(result2.drafts), 0)
        event.refresh_from_db()
        self.assertEqual(event.deadline, aware(2026, 10, 25, 23, 59))

        superseded = event.fields.filter(field=FIELD_DEADLINE, is_current=False).first()
        self.assertIsNotNone(superseded)
        self.assertEqual(superseded.value_dt, aware(2026, 10, 20, 23, 59))
        self.assertEqual(superseded.superseded_by.field, FIELD_DEADLINE)
        # 旧的当前值只剩一个，保证「以哪一版为准」唯一
        self.assertEqual(event.fields.filter(field=FIELD_DEADLINE, is_current=True).count(), 1)

        # 延期后提醒必须重排，否则用户会被引到旧时间点
        reminder = event.reminders.first()
        self.assertEqual(reminder.remind_at, aware(2026, 10, 24, 23, 59))
        self.assertNotEqual(reminder.remind_at, old_remind_at)

    def test_duplicate_notice_across_sources_merges(self):
        raw1 = "【程序设计竞赛报名】\n请于10月20日前完成报名，报名链接：https://example.edu/contest"
        _batch1, result1 = self._run(raw1)
        event = result1.drafts[0]
        confirm_event(event, reminder_offsets=[])

        raw2 = "【程序设计竞赛报名】\n请于10月20日前完成报名，报名链接：https://example.edu/contest\n主办：校团委"
        _batch2, result2 = self._run(raw2)
        self.assertEqual(len(result2.merged), 1)
        event.refresh_from_db()
        self.assertEqual(event.links.count(), 2)

    def test_gray_zone_asks_user_instead_of_silently_merging(self):
        _batch1, result1 = self._run("【高等数学作业】请于10月20日前提交")
        first = result1.drafts[0]
        confirm_event(first, reminder_offsets=[])

        _batch2, result2 = self._run("高等数学作业提交提醒")
        self.assertEqual(len(result2.merged), 0)
        draft = result2.drafts[0]
        self.assertIsNotNone(draft.review_candidate)
        self.assertTrue(draft.needs_review)
        self.assertIn("疑似", draft.review_note)

    def test_noise_only_batch_produces_nothing(self):
        _batch, result = self._run("收到\n好的\n哈哈哈")
        self.assertEqual(result.processed, [])
        self.assertTrue(result.warnings)

    def test_extraction_run_is_recorded(self):
        from extraction.models import ExtractionRun

        self._run("【通知】10月20日前提交实验报告")
        run = ExtractionRun.objects.filter(owner=self.user).first()
        self.assertIsNotNone(run)
        self.assertEqual(run.provider, "rules")
        self.assertEqual(run.event_count, 1)

    def test_cancel_notice_marks_event_cancelled(self):
        _batch1, result1 = self._run("【程序设计竞赛报名】\n请于10月20日前完成报名")
        event = result1.drafts[0]
        confirm_event(event, reminder_offsets=[])
        self._run("更正：程序设计竞赛报名取消")
        event.refresh_from_db()
        self.assertTrue(event.is_cancelled)

    def test_reimport_of_same_text_reuses_event_instead_of_new_draft(self):
        """重复导入 / 重新识别必须并回原事项，不能凭空多出一条重复草稿。"""
        from ingest.services import DuplicateImport, create_batch as make_batch

        raw = "【程序设计竞赛报名】\n请于10月20日前完成报名，报名链接：https://example.edu/contest"
        _batch1, result1 = self._run(raw)
        event = result1.drafts[0]

        # 默认拦截并说明原因
        with self.assertRaises(DuplicateImport):
            make_batch(self.user, raw)

        # 强制重新识别 → 复用原文，并回到同一个事项
        batch2, messages2, _ = make_batch(self.user, raw, reuse_existing=True)
        result2 = process_batch(batch2)
        self.assertEqual(len(result2.drafts), 0)
        self.assertEqual(len(result2.merged), 1)
        self.assertEqual(result2.merged[0].event.pk, event.pk)
        self.assertEqual(Event.objects.filter(owner=self.user).count(), 1)
        # 同一原文只保留一个关联，历史里也不会多出无意义的记录
        event.refresh_from_db()
        self.assertEqual(event.links.count(), 1)
        self.assertEqual(event.fields.filter(field=FIELD_DEADLINE).count(), 1)

    def test_reimport_of_confirmed_event_refreshes_without_duplicating(self):
        raw = "【程序设计竞赛报名】\n请于10月20日前完成报名"
        _batch1, result1 = self._run(raw)
        event = result1.drafts[0]
        confirm_event(event, reminder_offsets=[1440])

        from ingest.services import create_batch as make_batch

        batch2, _messages, _ = make_batch(self.user, raw, reuse_existing=True)
        process_batch(batch2)
        self.assertEqual(Event.objects.filter(owner=self.user).count(), 1)
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.CONFIRMED)
        self.assertEqual(event.reminders.count(), 1)

    def test_corrected_location_actually_replaces_old_value(self):
        """回归：字段级「是否更正」必须按字段所属消息判断。

        规则抽取器早期没标注字段来自哪条消息，导致「更正：地点改到X」被误判为
        「不是更正 → 保留原值」，事件上的地点会停在旧值。
        """
        raw = (
            "学习委员 2026-10-08 09:12\n"
            "【程序设计竞赛报名】\n"
            "请于10月20日前完成报名，地点：实验楼B302\n"
            "学习委员 2026-10-08 09:15\n"
            "更正：报名地点改到实验楼B303"
        )
        _batch, result = self._run(raw)
        event = result.merged[0].event
        event.refresh_from_db()
        self.assertEqual(event.location, "实验楼B303")
        current = event.current_field(FIELD_LOCATION)
        self.assertEqual(current.value_text, "实验楼B303")
        self.assertEqual(event.fields.filter(field=FIELD_LOCATION, is_current=False).count(), 1)
        self.assertEqual(event.fields.filter(field=FIELD_LOCATION, is_current=True).count(), 1)
        # 冗余列与字段一致（列表/搜索读的是冗余列）
        self.assertEqual(event.current_field(FIELD_LOCATION).value_text, event.location)

    def test_relative_time_uses_each_messages_own_timestamp(self):
        """回归：一批消息跨天时，相对时间要按各自消息的发送时间换算。"""
        raw = (
            "王老师 2026-10-08 20:00\n"
            "【高数作业】\n"
            "请于10月20日前提交第三章习题\n"
            "王老师 2026-10-15 20:00\n"
            "补充：实验报告改成明天中午前交"
        )
        _batch, result = self._run(raw)
        self.assertTrue(result.processed)
        event = result.processed[-1].event
        # 第二条消息的「明天」= 10-16，而不是第一条消息日期推算出的 10-09
        field = event.current_field(FIELD_DEADLINE)
        self.assertIsNotNone(field, "应识别出补充消息里的截止时间")
        self.assertEqual(field.value_dt.date(), date(2026, 10, 16))


class FakeMessage:
    """联调用例的输入对象：Provider 只读这几个属性，不必真的落库。"""

    def __init__(self, seq, text, sender="", source_label="测试群", anchor_time=None):
        from django.utils import timezone

        self.seq = seq
        self.raw_text = text
        self.sender = sender
        self.source_label = source_label
        self.anchor_time = anchor_time or timezone.now()


@unittest.skipUnless(os.environ.get("LLM_LIVE_TEST") == "1", "需要真实调用大模型：LLM_LIVE_TEST=1 才跑")
class LiveLlmTests(TestCase):
    """真实调用识别服务的联调用例（默认跳过，避免单测联网/花钱/不确定）。

    运行：LLM_LIVE_TEST=1 python manage.py test extraction.tests.LiveLlmTests
    """

    def _fake_message(self, seq, text, sender="", source_label="测试群"):
        return FakeMessage(seq, text, sender=sender, source_label=source_label)

    def test_real_provider_merges_and_ignores_chitchat(self):
        from extraction.providers import OpenAICompatProvider

        messages = [
            self._fake_message(1, "【程序设计竞赛报名】\n请于10月20日前完成报名，地点：实验楼B302", "学习委员"),
            self._fake_message(2, "更正：报名地点改到实验楼B303", "学习委员"),
            self._fake_message(3, "收到", "同学A"),
        ]
        outcome = OpenAICompatProvider().extract(messages)
        self.assertEqual(outcome.provider, "llm")
        self.assertTrue(outcome.events, "模型应至少产出一个事件")
        self.assertEqual(len(outcome.events), 1, "两条消息应归并为同一事件")
        event = outcome.events[0]
        relations = {relation.relation for relation in event.messages}
        self.assertIn("correction", relations)
        location = event.get("location")
        self.assertIsNotNone(location, "应抽取出地点")
        self.assertIn("B303", location.value)
        self.assertEqual(len(outcome.ignored), 1, "「收到」应被判为闲聊")

    def test_real_provider_splits_milestones_of_one_notice(self):
        """多阶段通知必须拆成两个 events：提交阶段（deadline）与现场阶段（start_at）。

        这是提示词里最容易退化的一条：合成一件时，提醒按「截止优先」只排一套，
        课堂汇报会彻底没有提醒，而界面上却看不出少了什么。
        """
        from extraction.providers import OpenAICompatProvider

        text = (
            "软件设计小组作业提交通知\n"
            "各组请于【10月15日 18:00】前提交： ① 汇报PPT（命名：第X组+组长姓名） "
            "② 小组分工说明（含每人贡献占比）\n"
            "课堂汇报时间：【10月17日】，每组【8分钟】汇报+【2分钟】提问。请提前调试设备，控制好时间。"
        )
        outcome = OpenAICompatProvider().extract(
            [self._fake_message(1, text, "张老师", "软件设计课程群")]
        )
        self.assertEqual(
            len(outcome.events), 2, "提交阶段与课堂汇报阶段应拆成两个事项"
        )

        kinds = [
            {item.field for item in event.fields if item.field in ("deadline", "start_at", "end_at")}
            for event in outcome.events
        ]
        self.assertIn({"deadline"}, kinds, "应有一个只带截止时间的事件（提交）")
        self.assertIn({"start_at"}, kinds, "应有一个只带开始时间的事件（课堂汇报）")
        self.assertEqual(
            len({event.category for event in outcome.events}),
            1,
            "同一份作业的多个阶段应属于同一分类",
        )

        for event in outcome.events:
            self.assertNotIn("10月", event.title, "【】里的时间是强调符，不能当标题")
            self.assertIn("软件设计", event.title, "标题应能看出是哪门课的哪件事")
            course = event.get("course")
            if course is not None:
                self.assertEqual(course.value, "软件设计", "课程名不带「小组」「作业」等字样")
