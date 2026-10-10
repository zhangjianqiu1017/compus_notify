"""派发到点提醒（站内通知 + 邮件）。

部署时用计划任务周期调用：

    python manage.py dispatch_reminders

它负责在用户没打开页面时把提醒邮件发出去；用户打开页面或轮询
`/api/notifications/` 时也会派发一次（见 reminders.views / core.api），
因此即使不配置计划任务，提醒也不会丢失，只是延迟到下次访问（NFR-04）。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from reminders.services import dispatch_due


class Command(BaseCommand):
    help = "派发所有到点的提醒并发送邮件（含离线补偿），建议每分钟执行一次"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--username", help="仅派发指定用户的提醒")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只报告到点数量，不派发也不发信",
        )

    def handle(self, *args, **options) -> None:
        user = None
        username = options.get("username")
        if username:
            from django.contrib.auth import get_user_model

            user = get_user_model().objects.filter(username=username).first()
            if user is None:
                self.stderr.write(f"未找到用户 {username}")
                return

        if options.get("dry_run"):
            from django.utils import timezone

            from reminders.models import Reminder

            queryset = Reminder.objects.filter(
                is_enabled=True, status=Reminder.Status.SCHEDULED, remind_at__lte=timezone.now()
            )
            if user is not None:
                queryset = queryset.filter(owner=user)
            self.stdout.write(f"到点待派发：{queryset.count()} 条（dry-run，未发送）")
            return

        count = dispatch_due(user=user)
        sent = getattr(dispatch_due, "last_emails_sent", 0)
        self.stdout.write(
            self.style.SUCCESS(f"已派发 {count} 条提醒，其中邮件 {sent} 封")
        )
