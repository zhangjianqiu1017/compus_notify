"""生成演示数据，便于手动体验完整流程。

    python manage.py seed_demo --username demo

会创建一个演示账号并导入若干条真实风格的通知（含「多条消息描述同一事件」的场景）。
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from events.services import confirm_event
from extraction.pipeline import process_batch
from ingest.services import create_batch

DEMO_BATCHES = [
    (
        "软件2201班群",
        "paste",
        (
            "学习委员 2026-10-08 09:12\n"
            "【程序设计竞赛报名】\n"
            "请于10月20日前完成报名，报名链接：https://example.edu/contest\n"
            "地点：实验楼B302\n"
            "联系人：李老师\n"
            "要求：请携带学生证"
        ),
    ),
    (
        "软件2201班群",
        "paste",
        "学习委员 2026-10-08 09:15\n更正：报名地点改到实验楼B303，截止时间不变",
    ),
    (
        "高等数学课程群",
        "paste",
        "王老师 2026-10-09 20:03\n【高等数学作业】\n请于10月15日23:59前提交第三章习题，逾期不候",
    ),
    (
        "教务处公众号",
        "share",
        (
            "教务处 2026-10-10 08:30\n"
            "关于2026年秋季期末考试安排的通知\n"
            "考试时间：2026年11月2日 09:00-11:00\n"
            "考试地点：第三教学楼A201\n"
            "请携带学生证与黑色签字笔"
        ),
    ),
    (
        "社团联合会",
        "paste",
        "社联小助手 2026-10-11 12:00\n【校园歌手大赛】\n报名截止时间：10月30日\n初赛：11月5日 18:30 大学生活动中心\n注意：迟到15分钟视为弃权",
    ),
]

# 第一条导入后会被确认（模拟用户已完成确认），后续消息用于演示「更正 → 自动归并 → 提醒重排」
CONFIRM_FIRST = True


class Command(BaseCommand):
    help = "生成演示账号与演示通知数据"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--username", default="demo")
        parser.add_argument("--password", default="DemoPass!2026")

    def handle(self, *args, **options) -> None:
        username = options["username"]
        password = options["password"]
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        if created:
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f"已创建演示账号 {username} / {password}"))
        else:
            self.stdout.write(f"复用已有账号 {username}")

        total_events = 0
        for index, (label, source, raw) in enumerate(DEMO_BATCHES):
            try:
                batch, _messages, warnings = create_batch(user, raw, source=source, source_label=label)
            except ValueError as exc:
                self.stdout.write(f"跳过第 {index + 1} 批：{exc}")
                continue
            result = process_batch(batch)
            for warning in warnings + result.warnings:
                self.stdout.write(self.style.WARNING(f"  · {warning}"))

            if index == 0 and CONFIRM_FIRST:
                for event in result.drafts:
                    confirm_event(event, as_todo=True, as_calendar=True)
                    total_events += 1
                    self.stdout.write(f"  已确认：{event.title}")
            else:
                for item in result.processed:
                    action = {"created": "新建待确认", "review": "待确认(疑似重复)", "merged": "自动归并"}[item.action]
                    total_events += 1
                    self.stdout.write(f"  {action}：{item.event.title}")

        drafts = user.events.filter(status="draft").count()
        self.stdout.write(
            self.style.SUCCESS(
                f"演示数据完成：共 {total_events} 个事项，其中 {drafts} 个待在确认页处理。"
                f"（{timezone.localtime().strftime('%Y-%m-%d %H:%M')}）"
            )
        )
