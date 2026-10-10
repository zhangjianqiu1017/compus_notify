#!/usr/bin/env python
"""校园通知智能管理 APP —— Django 命令行入口。"""
from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - 环境缺失时给出人话提示
        raise ImportError(
            "无法导入 Django，请先执行：pip install -r requirements.txt"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
