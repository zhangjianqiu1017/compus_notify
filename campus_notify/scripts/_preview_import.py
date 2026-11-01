"""把「导入通知」页离线渲染成一张可直接截图的 HTML（仅本地视觉核对用，不进产品流程）。

用法：python scripts/_preview_import.py <输出目录>
会临时建一个用户渲染页面，渲染完立刻删除，不留数据。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else BASE / "_preview")
out_dir.mkdir(parents=True, exist_ok=True)

SAMPLE = (
    "张三 2026-10-08 09:12\n"
    "【程序设计竞赛报名】\n"
    "请于10月20日前完成报名，报名链接：https://example.edu/contest\n"
    "\n"
    "张三 2026-10-08 09:15\n"
    "补充：报名地点改到实验楼B302"
)

User = get_user_model()
client = Client()
user = None
try:
    user = User.objects.create_user(username="__preview__", email="preview@example.com", password="preview-1234")
    client.force_login(user)
    resp = client.get("/ingest/")
    assert resp.status_code == 200, resp.status_code
    html = resp.content.decode("utf-8")
finally:
    if user is not None:
        user.delete()

css = (BASE / "static" / "css" / "app.css").read_text(encoding="utf-8")
js = (BASE / "static" / "js" / "app.js").read_text(encoding="utf-8")

# 离线版：把外链样式/脚本换成内联，避免 file:// 下的相对路径问题
import re  # noqa: E402

html = re.sub(r'<link rel="stylesheet"[^>]*>', "<style>\n" + css + "\n</style>", html, count=1)
html = re.sub(r'<script src="[^"]*app\.js[^"]*" defer></script>', "<script>\n" + js + "\n</script>", html)
html = re.sub(r'<link rel="manifest"[^>]*>', "", html)
html = re.sub(r'<link rel="icon"[^>]*>', "", html)
# 关掉会真的发请求的轮询提示接口
html = html.replace("fetch(\"/api/notifications/\"", "fetch(\"about:blank#skipped\"")

(out_dir / "import_empty.html").write_text(html, encoding="utf-8")

filled = html.replace(
    '<textarea name="text" id="text" rows="10"',
    '<textarea name="text" id="text" rows="10"',
)
# 带内容的版本：把示例原文塞进 textarea
filled = re.sub(
    r'(<textarea name="text" id="text" rows="10"[^>]*>)',
    r"\1" + SAMPLE.replace("&", "&amp;").replace("<", "&lt;"),
    filled,
    count=1,
)
(out_dir / "import_filled.html").write_text(filled, encoding="utf-8")
print("ok:", out_dir)
