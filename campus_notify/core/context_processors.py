from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from reminders.models import InAppNotification


@lru_cache(maxsize=8)
def _static_version(_bucket: str) -> str:
    """静态资源指纹 = static 目录下最新的修改时间。

    开发时 Django 的静态响应不带 Cache-Control，浏览器会按启发式规则把 CSS/JS
    缓存住，改了样式刷新也看不到。给 URL 加个版本号即可强制重新拉取。
    （生产环境建议直接用 ManifestStaticFilesStorage 的哈希文件名。）
    """
    latest = 0
    for root in getattr(settings, "STATICFILES_DIRS", []) or []:
        try:
            for path in Path(root).rglob("*"):
                if path.is_file():
                    latest = max(latest, path.stat().st_mtime_ns)
        except OSError:  # pragma: no cover - 目录不可读时退化为固定值
            continue
    return str(latest)


def static_version() -> str:
    # 按秒分桶缓存：同一秒内不重复扫描目录，改动最多 1 秒后生效
    return _static_version(str(int(time.time())))


def app_context(request) -> dict:
    """所有模板都能拿到的基础上下文（未登录用户返回安全默认值）。"""
    context = {
        "app_name": "校园通知助手",
        "ads_enabled": bool(getattr(settings, "ENABLE_ADS", False)),
        "unread_notifications": 0,
        "extraction_mode": getattr(settings, "EXTRACTION_PROVIDER", "auto"),
        "llm_configured": bool(getattr(settings, "LLM_API_KEY", "")),
        "static_version": static_version(),
    }
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        context["unread_notifications"] = InAppNotification.objects.filter(
            owner=user, read_at__isnull=True
        ).count()
    return context
