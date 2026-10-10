"""测试公共辅助。

放在项目根目录而不是某个 app 内，避免被 Django 的测试发现机制当成某个 app 的测试模块。
"""
from __future__ import annotations

from datetime import datetime

from django.contrib.auth import get_user_model
from django.utils import timezone


def make_user(username: str = "tester", password: str = "Passw0rd!234"):
    return get_user_model().objects.create_user(username=username, password=password)


def aware(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """构造当前时区的 aware 时间。"""
    naive = datetime(year, month, day, hour, minute)
    return timezone.make_aware(naive, timezone.get_current_timezone())
