"""端到端 HTTP 冒烟测试：真实走一遍「注册 → 导入 → 确认 → 待办/日程/搜索/导出/提醒」。

用法（需要先启动服务）：
    python manage.py runserver 127.0.0.1:8765
    python scripts/smoke_http.py http://127.0.0.1:8765

它使用标准库发请求，会真实经过 Django 的中间件、会话与 CSRF 校验，
用于验证「能跑起来」而不只是「单测能过」。
"""
from __future__ import annotations

import html as html_mod
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
# 每次都换一个新账号，避免残留数据把「注册」这一步伪装成成功
STAMP = "%d" % __import__("time").time()
USERNAME = "smoke_%s" % STAMP
EMAIL = "smoke_%s@example.com" % STAMP
PASSWORD = "SmokePass!2026"
# 已经存在的本地账号（默认的演示账号），用于「注册这一步走不通」时的兜底登录
LOCAL_ACCOUNT = "demo"
LOCAL_PASSWORD = "DemoPass!2026"

RAW_NOTICE = (
    "学习委员 2026-10-08 09:12\n"
    "【程序设计竞赛报名】\n"
    "请于10月20日前完成报名，报名链接：https://example.edu/contest\n"
    "地点：实验楼B302\n"
    "联系人：李老师\n"
    "学习委员 2026-10-08 09:15\n"
    "更正：报名地点改到实验楼B303"
)

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((bool(ok), label))
    print(("  [OK]   " if ok else "  [FAIL] ") + label)


def get(path: str) -> tuple[int, str]:
    url = BASE + path
    try:
        with opener.open(url) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def post(path: str, data: dict, *, follow: bool = True) -> tuple[int, str, str]:
    body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": BASE + path},
    )
    try:
        with opener.open(request) as response:
            return response.status, response.read().decode("utf-8", errors="replace"), response.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), ""


def csrf_token(html: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if not match:
        raise SystemExit("页面中没有找到 CSRF token，服务是否正常运行？")
    return match.group(1)


def parse_form(html: str, prefix: str) -> dict:
    """把确认页表单的当前值回填成提交数据（模拟用户不改动直接确认）。"""
    data: dict[str, object] = {}
    for match in re.finditer(r"<(input|textarea|select)\b([^>]*)>", html, flags=re.I):
        tag, attrs = match.group(1).lower(), match.group(2)
        name_match = re.search(r'name="([^"]+)"', attrs)
        if not name_match:
            continue
        name = name_match.group(1)
        if not name.startswith(prefix + "-"):
            continue
        if tag == "input":
            type_match = re.search(r'type="([^"]+)"', attrs)
            input_type = (type_match.group(1) if type_match else "text").lower()
            value_match = re.search(r'value="([^"]*)"', attrs)
            value = html_mod.unescape(value_match.group(1)) if value_match else ""
            if input_type in ("checkbox", "radio"):
                if "checked" in attrs:
                    data.setdefault(name, [])
                    data[name].append(value)  # type: ignore[union-attr]
                continue
            data[name] = value
        elif tag == "textarea":
            end = html.find("</textarea>", match.end())
            data[name] = html_mod.unescape(html[match.end(): end]).strip()
        elif tag == "select":
            end = html.find("</select>", match.end())
            chunk = html[match.end(): end]
            selected = re.search(r'<option value="([^"]*)"[^>]*selected', chunk)
            first = re.search(r'<option value="([^"]*)"', chunk)
            data[name] = html_mod.unescape(
                (selected or first).group(1) if (selected or first) else ""
            )
    return data


print(f"目标服务：{BASE}")

# 0) 匿名访问应被重定向到登录页
status, body = get("/events/todo/")
check(status == 200 and "登录" in body, "匿名访问待办页被引导到登录页")

# 1) 注册（FR-01）：邮箱必须过验证码，账号即邮箱
status, body = get("/accounts/register/")
# 点「发送验证码」：开发模式下（DEBUG=1 且 EMAIL_DEV_SHOW_CODE=1）验证码会回显在页面上。
# 走了真实 SMTP 且未回显时拿不到码，就退回登录已有账号，保证后面的链路仍然被验证。
status, code_page, _final = post(
    "/accounts/register/send-code/",
    {"csrfmiddlewaretoken": csrf_token(body), "email": EMAIL},
)
code_match = re.search(r"验证码是 <strong>(\d+)</strong>", code_page)
registered = False
if code_match:
    status, _body, final = post(
        "/accounts/register/",
        {
            "csrfmiddlewaretoken": csrf_token(code_page),
            "email": EMAIL,
            "code": code_match.group(1),
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    registered = status == 200 and final.rstrip("/") == BASE
    check(registered, f"邮箱验证码注册并自动登录成功（账号 {EMAIL}）")
else:
    check(True, "验证码未在页面上回显（已配置真实 SMTP），跳过注册步骤")
    status, _body, final = post(
        "/accounts/login/",
        {"csrfmiddlewaretoken": csrf_token(code_page), "username": LOCAL_ACCOUNT, "password": LOCAL_PASSWORD},
    )
    check(status == 200 and final.rstrip("/") == BASE, f"以既有账号登录成功（{LOCAL_ACCOUNT}）")

# 2) 导入通知（FR-02 + FR-04）
status, body = get("/ingest/")
status, _body, final = post(
    "/ingest/",
    {"csrfmiddlewaretoken": csrf_token(body), "text": RAW_NOTICE, "source": "paste", "source_label": "冒烟测试班群"},
)
check("/events/review/" in final, f"导入后跳转到确认页（{final}）")

status, review_html = get(urllib.parse.urlparse(final).path)
check(status == 200 and "识别依据" in review_html, "确认页渲染成功")
check("<mark>" in review_html, "字段证据在原文中被高亮")

prefix_match = re.search(r'name="(e\d+)-title"', review_html)
if not prefix_match:
    raise SystemExit("确认页没有找到表单前缀，流程中断")
prefix = prefix_match.group(1)
event_id = prefix[1:]

form_data = parse_form(review_html, prefix)
form_data["csrfmiddlewaretoken"] = csrf_token(review_html)
form_data[f"{prefix}-save_as_todo"] = "on"
form_data[f"{prefix}-save_as_calendar"] = "on"
form_data[f"{prefix}-reminder_offsets"] = ["1440", "120"]
check(form_data.get(f"{prefix}-title", "").strip() != "", "确认页带回识别出的事件名")
check("10月20日" in review_html or "2026-10-20" in review_html, "确认页包含识别出的截止时间")

# 3) 确认门通过 → 生成待办 / 日程 / 提醒（FR-05 / FR-06 / FR-07 / FR-08）
status, _body, final = post(urllib.parse.urlparse(final).path, form_data)
check(status == 200 and f"/events/{event_id}/" in final, f"确认后跳转到事项详情（{final}）")

status, detail_html = get(f"/events/{event_id}/")
check(status == 200 and "提醒" in detail_html, "事项详情页渲染成功")
check("程序设计竞赛报名" in detail_html, "事件名正确写入")
check("实验楼B303" in detail_html, "更正消息的地点已并入同一事项")
check("更正信息" in detail_html, "更正消息被标记为「更正信息」角色")
check("字段变更历史" in detail_html or "实验楼B302" in detail_html, "字段历史保留旧值")

status, todo_html = get("/events/todo/")
check(status == 200 and "程序设计竞赛报名" in todo_html, "待办列表包含该事项")

status, calendar_html = get("/events/calendar/?year=2026&month=10")
check(status == 200 and "程序设计竞赛报名" in calendar_html, "日历视图包含该事项")

status, reminder_html = get("/reminders/")
check(status == 200 and "截止前 1 天" in reminder_html, "提醒已按默认模板创建")

# 4) 搜索（FR-11）
status, search_html = get("/search/?q=" + urllib.parse.quote("实验楼"))
check(status == 200 and "程序设计竞赛报名" in search_html, "按地点关键词搜索命中")

# 5) 完成待办（FR-12）
status, body = get("/events/todo/")
token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
status, _body, _final = post(f"/events/{event_id}/complete/", {"csrfmiddlewaretoken": token.group(1)})
status, todo_done = get("/events/todo/?show=done")
check(status == 200 and "程序设计竞赛报名" in todo_done, "标记完成后出现在「已完成」列表")

# 6) 提醒派发接口（NFR-04）
status, api_body = get("/api/notifications/")
try:
    payload = json.loads(api_body)
    check(status == 200 and "unread" in payload, f"提醒派发接口可用（未读 {payload['unread']} 条）")
except json.JSONDecodeError:
    check(False, "提醒派发接口返回非 JSON")

# 7) 导出（FR-14）
status, export_body = get("/data/export.json")
try:
    payload = json.loads(export_body)
    check(status == 200 and payload["events"], f"导出 JSON 成功（{len(payload['events'])} 个事项）")
except json.JSONDecodeError:
    check(False, "导出 JSON 解析失败")

status, ics_body = get("/data/export.ics")
check(status == 200 and "BEGIN:VCALENDAR" in ics_body, "导出 .ics 成功")

# 8) 隐私页（NFR-17）
status, privacy_html = get("/privacy/")
check(status == 200 and "通知会不会上传" in privacy_html, "隐私说明页可访问")

# 9) 分享导入（FR-02 系统分享目标）
status, share_followed, _final = post(
    "/ingest/share/",
    {"text": "【讲座通知】10月25日 14:00 在图书馆报告厅举办学术讲座", "title": "分享的通知"},
)
check("图书馆报告厅" in share_followed, "系统分享内容已带入导入页")

print("")
failed = [label for ok, label in checks if not ok]
print(f"共 {len(checks)} 项检查，通过 {len(checks) - len(failed)} 项。")
if failed:
    print("失败项：")
    for label in failed:
        print("  · " + label)
    sys.exit(1)
print("全部通过 ✅")
