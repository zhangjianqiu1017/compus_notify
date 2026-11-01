"""把指定页面用测试客户端渲染成静态 HTML，静态资源改指本地文件，方便无头浏览器截图核对布局。

用法：python scripts/_preview_render.py <相对站点路径> <输出 html 路径> [宽/窄]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import django

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402


def main() -> None:
    site_path = sys.argv[1] if len(sys.argv) > 1 else "/"
    out_path = Path(sys.argv[2] if len(sys.argv) > 2 else BASE_DIR / "_preview.html")
    username = sys.argv[3] if len(sys.argv) > 3 else "demo"
    static_root = (BASE_DIR / "static").as_uri().rstrip("/")

    user = get_user_model().objects.filter(username=username).first() or get_user_model().objects.first()
    client = Client(HTTP_HOST="localhost")
    client.force_login(user)
    response = client.get(site_path)
    html = response.content.decode()

    html = html.replace('href="/static/', f'href="{static_root}/').replace('src="/static/', f'src="{static_root}/')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"status={response.status_code} -> {out_path}")


if __name__ == "__main__":
    main()
