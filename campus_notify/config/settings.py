"""校园通知智能管理 APP —— Django 配置。

设计说明（对应需求文档）：
* 隐私优先：原文可选择性保存（NFR-15），识别服务可替换（5.5-3）。
* 识别链路：批次抽取 → 时间归一 → 候选召回 → 三区间归并 → 冲突消解 → 用户确认。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from config.env import load_env

load_env()

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-please-change-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost", "[::1]"])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "ingest",
    "extraction",
    "events",
    "reminders",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# 账号即邮箱：允许用邮箱或用户名（历史账号）登录
AUTHENTICATION_BACKENDS = ["accounts.backends.EmailOrUsernameBackend"]

# --------------------------------------------------------------------------
# 邮件（用于注册绑定邮箱与找回密码的验证码）
# --------------------------------------------------------------------------
# 开发默认打控制台：验证码会出现在 runserver 的日志里，不需要真实邮箱服务。
# 生产改成 SMTP：
#   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
#   EMAIL_HOST=smtp.example.com  EMAIL_PORT=465  EMAIL_USE_SSL=1
#   EMAIL_HOST_USER=no-reply@example.com  EMAIL_HOST_PASSWORD=xxxx
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 15)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "校园通知助手 <no-reply@localhost>")

# 验证码策略
EMAIL_CODE_TTL_MINUTES = env_int("EMAIL_CODE_TTL_MINUTES", 10)
EMAIL_CODE_MAX_ATTEMPTS = env_int("EMAIL_CODE_MAX_ATTEMPTS", 5)
EMAIL_CODE_RESEND_SECONDS = env_int("EMAIL_CODE_RESEND_SECONDS", 60)
# 开发便利：把验证码直接显示在页面上。仅在 DEBUG=True 时可能生效（代码里强制判断），
# 生产环境（DEBUG=False）永远关闭。
EMAIL_DEV_SHOW_CODE = env_bool("EMAIL_DEV_SHOW_CODE", True)

# --------------------------------------------------------------------------
# 邮件提醒（FR-08）：提醒到点后发信给用户，而不是只在站内留一条通知
# --------------------------------------------------------------------------
# 总开关。关掉后只派发站内通知（本地演示 / 无 SMTP 的部署可用）。
EMAIL_REMINDER_ENABLED = env_bool("EMAIL_REMINDER_ENABLED", True)
# 提醒邮件的发件人，默认沿用 DEFAULT_FROM_EMAIL。
EMAIL_REMINDER_FROM = os.environ.get("EMAIL_REMINDER_FROM", "") or DEFAULT_FROM_EMAIL
# 单次派发的邮件上限：用户离线多日再上线时，不至于一次被十几封信埋掉。
# 超出上限的提醒仍会生成站内通知，邮件留到下次派发（不是丢弃）。
EMAIL_REMINDER_MAX_PER_DISPATCH = env_int("EMAIL_REMINDER_MAX_PER_DISPATCH", 5)
# 没有请求上下文时（计划任务 dispatch_reminders）用于拼邮件里链接的站点地址，
# 例如 https://notify.example.edu。留空则退回 django.contrib.sites 的当前站点。
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "").rstrip("/")

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

# NFR-13 传输加密：生产环境（DEBUG=False）强制 HTTPS 相关开关。
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --------------------------------------------------------------------------
# 业务配置：识别（抽取）服务
# --------------------------------------------------------------------------
# auto  -> 有 LLM_API_KEY 时用大模型，否则退回本地规则抽取器
# llm   -> 强制大模型（失败即报错，用于线上）
# rules -> 强制规则抽取器（离线开发 / 单测）
EXTRACTION_PROVIDER = os.environ.get("EXTRACTION_PROVIDER", "auto").strip().lower()

# 单测必须离线且结果确定：不能让开发机 .env 里的 key 影响断言，
# 也不能让测试真的发出邮件。
# 需要真实调用大模型的用例请显式设置 LLM_LIVE_TEST=1 单独跑（见 LiveLlmTests）。
if "test" in sys.argv or "pytest" in Path(sys.argv[0]).name:
    EXTRACTION_PROVIDER = "rules"
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# OpenAI 兼容接口（DeepSeek / 通义 / vLLM 等均可）
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = env_int("LLM_TIMEOUT", 20)
LLM_MAX_TOKENS = env_int("LLM_MAX_TOKENS", 2000)
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.1"))

# 单次导入最多处理多少条消息（防止一次粘贴过大导致超时，NFR-01 3 秒）
MAX_MESSAGES_PER_BATCH = env_int("MAX_MESSAGES_PER_BATCH", 40)

# --------------------------------------------------------------------------
# 业务配置：事件归并（三区间决策）
# --------------------------------------------------------------------------
# score >= AUTO  : 自动合并（可撤销）
# NEW <= score < AUTO : 灰区 —— 不静默决定，交给用户确认（NFR-05）
# score < NEW    : 新建事件
MERGE_AUTO_THRESHOLD = float(os.environ.get("MERGE_AUTO_THRESHOLD", "0.86"))
MERGE_NEW_THRESHOLD = float(os.environ.get("MERGE_NEW_THRESHOLD", "0.55"))
MERGE_CANDIDATE_LIMIT = env_int("MERGE_CANDIDATE_LIMIT", 5)
# 灰区是否同步调用一次 LLM 复核（关闭时直接把疑问抛给用户）
MERGE_LLM_RECHECK = env_bool("MERGE_LLM_RECHECK", False)

# --------------------------------------------------------------------------
# 业务配置：时间归一
# --------------------------------------------------------------------------
# 校历第一周的周一，用于解析“第 N 周周三”
TERM_START_DATE = os.environ.get("TERM_START_DATE", "2026-09-07")
# 日期缺失时刻时的默认值（按字段）
DEFAULT_TIME_BY_FIELD = {
    "deadline": "23:59",
    "start_at": None,
    "end_at": None,
}

# --------------------------------------------------------------------------
# 业务配置：提醒
# --------------------------------------------------------------------------
# 默认提醒提前量（分钟），按锚点字段
REMINDER_DEFAULT_OFFSETS = {
    "deadline": [1440, 120],  # 截止前 1 天、2 小时
    "start_at": [30],         # 开始前 30 分钟
}
# 提醒派发时最多补偿多少天内的过期提醒（NFR-04 离线恢复补偿）
REMINDER_COMPENSATE_DAYS = env_int("REMINDER_COMPENSATE_DAYS", 3)

# MVP 无广告（需求文档 5.4 重要约束）
ENABLE_ADS = False
