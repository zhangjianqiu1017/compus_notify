"""一次性人工验收脚本：以 demo 账号走 HTTP，检查「提醒送达」相关页面。

    python manage.py runserver 127.0.0.1:8767
    python scripts/_uat_email_check.py http://127.0.0.1:8767

不属于正式测试（正式测试是 manage.py test 与 scripts/smoke_http.py），
仅用于改完邮件提醒后快速确认页面渲染与收件地址回显。
"""
from __future__ import annotations

import http.cookiejar
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8767").rstrip("/")
ACCOUNT, PASSWORD = "demo", "DemoPass!2026"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch(path: str) -> tuple[int, str]:
    try:
        with opener.open(BASE + path) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def token(html: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if not match:
        raise SystemExit("未找到 CSRF token")
    return match.group(1)


def submit(path: str, data: dict, referer_html: str) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": BASE + path},
    )
    with opener.open(request) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


_status, login_html = fetch("/accounts/login/")
submit("/accounts/login/", {"csrfmiddlewaretoken": token(login_html), "username": ACCOUNT, "password": PASSWORD}, login_html)

for path, needles in [
    ("/reminders/", ["发到你的邮箱", "提醒"]),
    ("/reminders/notifications/", ["邮件已发", "/accounts/profile/"]),
    ("/accounts/profile/", ["提醒同时发到我的邮箱", "收件邮箱"]),
    ("/privacy/", ["提醒邮件里有什么"]),
]:
    status, html = fetch(path)
    missing = [needle for needle in needles if needle not in html]
    print(f"{'[OK]  ' if status == 200 and not missing else '[FAIL]'} {path} status={status} 缺失={missing}")
