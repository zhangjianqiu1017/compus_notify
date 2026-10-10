"""提醒编排与派发。

提醒到点后的送达通道有两条，同一次派发里一起走：

1. **邮件**（主通道）：用户不用打开页面就能收到，是「提醒」真正起作用的地方；
2. **站内通知**：留痕 + 兜底，顶栏角标与「提醒 → 通知」页读的就是它。

邮件发送失败不会影响站内通知，也不会把提醒标成已派发——
失败的通知 `email_sent_at` 留空，下一次派发会重试（见 `send_reminder_email`）。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from events.models import Event
from reminders.models import InAppNotification, Reminder

logger = logging.getLogger(__name__)

# 邮件正文里允许出现的字段长度上限，避免一封提醒信里塞进整段原文
EMAIL_TEXT_LIMIT = 200
FOOTER_SEPARATOR = "—" * 12


def anchor_for_event(event) -> tuple[str, object] | tuple[None, None]:
    """提醒锚点：截止时间优先，其次是开始时间。"""
    if event.deadline:
        return Reminder.Anchor.DEADLINE, event.deadline
    if event.start_at:
        return Reminder.Anchor.START_AT, event.start_at
    return None, None


def default_offsets(anchor_kind: str) -> list[int]:
    table = getattr(settings, "REMINDER_DEFAULT_OFFSETS", {}) or {}
    return [int(item) for item in table.get(anchor_kind, [])]


def label_for(offset_minutes: int, anchor_kind: str) -> str:
    anchor = "截止" if anchor_kind == Reminder.Anchor.DEADLINE else "开始"
    minutes = int(offset_minutes)
    if minutes == 0:
        return f"{anchor}时提醒"
    if minutes % 1440 == 0:
        return f"{anchor}前 {minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{anchor}前 {minutes // 60} 小时"
    return f"{anchor}前 {minutes} 分钟"


def _preference_allows_default(user) -> bool:
    from accounts.models import UserPreference

    return UserPreference.for_user(user).default_reminder_enabled


def sync_reminders(event, *, offsets=None, force: bool = False) -> list[Reminder]:
    """按事件当前时间字段重排提醒。

    * offsets 为 None 时沿用该事件已配置的提前量（没有则用默认模板）；
    * 事件被取消、已完成或未确认时，全部提醒转为「已取消」；
    * 时间被更正后，已触发的提醒会被重置为待触发并重新排期。
    """
    existing = list(event.reminders.all())

    inactive = (
        event.is_cancelled
        or event.status != event.Status.CONFIRMED
        or event.todo_status == event.TodoStatus.DONE
    )
    if inactive:
        event.reminders.exclude(status=Reminder.Status.CANCELLED).update(
            status=Reminder.Status.CANCELLED, is_enabled=False, updated_at=timezone.now()
        )
        return []

    anchor_kind, anchor = anchor_for_event(event)
    if anchor is None:
        event.reminders.exclude(status=Reminder.Status.CANCELLED).update(
            status=Reminder.Status.CANCELLED, is_enabled=False, updated_at=timezone.now()
        )
        return []

    if offsets is None:
        configured = sorted({item.offset_minutes for item in existing})
        if not configured and _preference_allows_default(event.owner):
            configured = default_offsets(anchor_kind)
    else:
        configured = sorted({int(item) for item in offsets})

    now = timezone.now()
    keep: list[Reminder] = []
    for offset in configured:
        remind_at = anchor - timedelta(minutes=offset)
        if remind_at < now:
            # 已经过点的提前量：落到「现在」触发，作为离线/迟确认的补偿
            remind_at = now
        reminder, created = Reminder.objects.get_or_create(
            event=event,
            anchor_kind=anchor_kind,
            offset_minutes=offset,
            defaults={
                "owner": event.owner,
                "remind_at": remind_at,
                "label": label_for(offset, anchor_kind),
                "status": Reminder.Status.SCHEDULED,
                "is_enabled": True,
            },
        )
        changed = False
        if not created:
            if reminder.remind_at != remind_at:
                reminder.remind_at = remind_at
                changed = True
            if reminder.status != Reminder.Status.SCHEDULED:
                # 时间被更正：旧的已触发提醒作废重排
                reminder.status = Reminder.Status.SCHEDULED
                reminder.sent_at = None
                changed = True
            if not reminder.is_enabled:
                reminder.is_enabled = True
                changed = True
            reminder.label = label_for(offset, anchor_kind)
            if changed:
                reminder.save()
        keep.append(reminder)

    keep_ids = [item.pk for item in keep]
    stale = event.reminders.exclude(pk__in=keep_ids).exclude(status=Reminder.Status.CANCELLED)
    if force:
        stale.delete()
    else:
        stale.update(status=Reminder.Status.CANCELLED, is_enabled=False, updated_at=timezone.now())
    return keep


def set_reminder_offsets(event, offsets) -> list[Reminder]:
    """整组替换提醒提前量（保留给批量/脚本场景）。"""
    return sync_reminders(event, offsets=offsets, force=True)


# 界面上的提醒输入单位：分钟 / 小时 / 天
OFFSET_UNITS = {1: "分钟", 60: "小时", 1440: "天"}
OFFSET_UNIT_CHOICES = list(OFFSET_UNITS.items())
MAX_OFFSET_MINUTES = 60 * 24 * 30  # 最多提前 30 天


def offset_parts(minutes: int) -> tuple[int, int]:
    """把分钟数拆成 (数量, 单位)，供界面显示为「1 天」「2 小时」。"""
    minutes = int(minutes)
    for unit in (1440, 60):
        if minutes and minutes % unit == 0:
            return minutes // unit, unit
    return minutes, 1


def offset_from_parts(amount, unit) -> int | None:
    """界面输入 → 提前分钟数；非法输入返回 None。"""
    try:
        amount = int(amount)
        unit = int(unit)
    except (TypeError, ValueError):
        return None
    if amount < 0 or unit not in OFFSET_UNITS:
        return None
    minutes = amount * unit
    if minutes > MAX_OFFSET_MINUTES:
        return None
    return minutes


def _remind_at_for(anchor, offset_minutes: int, now=None):
    now = now or timezone.now()
    moment = anchor - timedelta(minutes=int(offset_minutes))
    # 已经过点的提前量落到「现在」触发，作为迟设置的补偿
    return moment if moment > now else now


def add_reminder(event, offset_minutes: int) -> Reminder:
    """新增一条提醒，不影响该事项的其他提醒（FR-08）。"""
    anchor_kind, anchor = anchor_for_event(event)
    if anchor is None:
        raise ValueError("该事项还没有截止或开始时间，先补上时间再设提醒。")
    offset_minutes = int(offset_minutes)
    if not 0 <= offset_minutes <= MAX_OFFSET_MINUTES:
        raise ValueError("提醒时间超出范围。")
    if Reminder.objects.filter(
        event=event, anchor_kind=anchor_kind, offset_minutes=offset_minutes
    ).exists():
        raise ValueError(f"已经有一条「{label_for(offset_minutes, anchor_kind)}」了。")
    return Reminder.objects.create(
        owner=event.owner,
        event=event,
        anchor_kind=anchor_kind,
        offset_minutes=offset_minutes,
        remind_at=_remind_at_for(anchor, offset_minutes),
        label=label_for(offset_minutes, anchor_kind),
        is_enabled=True,
        status=Reminder.Status.SCHEDULED,
    )


def update_reminder_offset(reminder: Reminder, offset_minutes: int) -> Reminder:
    """修改某一条提醒的提前量（FR-08）。"""
    offset_minutes = int(offset_minutes)
    if not 0 <= offset_minutes <= MAX_OFFSET_MINUTES:
        raise ValueError("提醒时间超出范围。")
    event = reminder.event
    anchor = event.deadline if reminder.anchor_kind == Reminder.Anchor.DEADLINE else event.start_at
    if anchor is None:
        anchor_kind, anchor = anchor_for_event(event)
        if anchor is None:
            raise ValueError("该事项还没有截止或开始时间，无法调整提醒。")
        reminder.anchor_kind = anchor_kind
    duplicate = (
        Reminder.objects.filter(event=event, anchor_kind=reminder.anchor_kind, offset_minutes=offset_minutes)
        .exclude(pk=reminder.pk)
        .exists()
    )
    if duplicate:
        raise ValueError(f"「{label_for(offset_minutes, reminder.anchor_kind)}」这条提醒已经存在了。")
    reminder.offset_minutes = offset_minutes
    reminder.label = label_for(offset_minutes, reminder.anchor_kind)
    reminder.remind_at = _remind_at_for(anchor, offset_minutes)
    reminder.status = Reminder.Status.SCHEDULED
    reminder.sent_at = None
    reminder.is_enabled = True
    reminder.save()
    return reminder


def toggle_reminder(reminder: Reminder, enabled: bool) -> Reminder:
    reminder.is_enabled = bool(enabled)
    if enabled and reminder.status == Reminder.Status.CANCELLED:
        reminder.status = Reminder.Status.SCHEDULED
        reminder.sent_at = None
        now = timezone.now()
        if reminder.remind_at < now:
            reminder.remind_at = now
    reminder.save()
    return reminder


def dispatch_due(*, user=None, now=None, request=None) -> int:
    """派发到点的提醒：站内通知 + 邮件提醒。

    返回「派发了几条提醒」，与调用方（页面、接口、计划任务）的既有语义一致；
    本次发出多少封邮件记在 `dispatch_due.last_emails_sent` 上。
    过期太久的不再打扰，直接作废。
    """
    now = now or timezone.now()
    compensate_days = int(getattr(settings, "REMINDER_COMPENSATE_DAYS", 3))
    horizon = now - timedelta(days=compensate_days)
    email_budget = max_emails_per_dispatch() if email_enabled() else 0

    queryset = Reminder.objects.select_related("event", "owner").filter(
        is_enabled=True, status=Reminder.Status.SCHEDULED, remind_at__lte=now
    )
    if user is not None:
        queryset = queryset.filter(owner=user)

    created = 0
    emails_sent = 0
    for reminder in queryset:
        event = reminder.event
        if event.is_cancelled or event.status != Event.Status.CONFIRMED or event.todo_status == Event.TodoStatus.DONE:
            reminder.status = Reminder.Status.CANCELLED
            reminder.is_enabled = False
            reminder.save(update_fields=["status", "is_enabled", "updated_at"])
            continue
        if reminder.remind_at < horizon:
            reminder.status = Reminder.Status.CANCELLED
            reminder.save(update_fields=["status", "updated_at"])
            continue

        anchor = event.deadline if reminder.anchor_kind == Reminder.Anchor.DEADLINE else event.start_at
        level = InAppNotification.Level.INFO
        body_parts = []
        if anchor:
            local = timezone.localtime(anchor).strftime("%m月%d日 %H:%M")
            if anchor < now:
                level = InAppNotification.Level.OVERDUE
                body_parts.append(f"已过时间：{local}")
            elif (anchor - now) <= timedelta(hours=6):
                level = InAppNotification.Level.WARN
                body_parts.append(f"即将到期：{local}")
            else:
                body_parts.append(f"时间：{local}")
        if event.location:
            body_parts.append(f"地点：{event.location}")
        if event.course:
            body_parts.append(f"课程：{event.course}")

        notification = InAppNotification.objects.create(
            owner=reminder.owner,
            reminder=reminder,
            event=event,
            title=f"{reminder.label or '提醒'}：{event.title}",
            body="　".join(body_parts)[:300],
            level=level,
        )
        if email_budget > 0 and _mail_pending(notification, request):
            emails_sent += 1
            email_budget -= 1
        reminder.status = Reminder.Status.SENT
        reminder.sent_at = now
        reminder.save(update_fields=["status", "sent_at", "updated_at"])
        created += 1

    dispatch_due.last_emails_sent = emails_sent
    return created


def unread_count(user) -> int:
    return InAppNotification.objects.filter(owner=user, read_at__isnull=True).count()


# ---------------------------------------------------------------------------
# 邮件提醒
# ---------------------------------------------------------------------------
def email_enabled() -> bool:
    """总开关：关掉后全站只派发站内通知（测试或本地演示时很有用）。"""
    return bool(getattr(settings, "EMAIL_REMINDER_ENABLED", True))


def max_emails_per_dispatch() -> int:
    """单次派发的邮件上限，防止一个迟到很久的账号一次性轰出几十封。"""
    return max(0, int(getattr(settings, "EMAIL_REMINDER_MAX_PER_DISPATCH", 5)))


def user_wants_email(user) -> bool:
    """用户偏好：关掉邮件后站内提醒照旧，只是不发信。"""
    from accounts.models import UserPreference

    return bool(UserPreference.for_user(user).reminder_email_enabled)


def recipient_for(user) -> str:
    """收件地址。

    账号体系里 `username` 存的就是邮箱（注册时强制绑定），历史账号的
    `email` 字段可能为空，因此这里兜底取 username。
    """
    candidate = (getattr(user, "email", "") or "").strip()
    if "@" not in candidate:
        candidate = (getattr(user, "username", "") or "").strip()
    return candidate if "@" in candidate else ""


def site_base_url(request=None) -> str:
    """站点根地址，用于拼邮件里的绝对链接。

    优先级：当前请求的域名（用户从哪个域名访问就用哪个）→ settings.SITE_BASE_URL
    → django.contrib.sites 的当前站点 → localhost 兜底。
    计划任务派发时没有请求对象，所以在生产环境建议显式配置 SITE_BASE_URL。
    """
    if request is not None:
        try:
            return request.build_absolute_uri("/").rstrip("/")
        except Exception:  # pragma: no cover - 极端环境下退回配置值
            logger.debug("build_absolute_uri 失败，改用站点配置拼接链接", exc_info=True)

    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    if base:
        return base

    from django.apps import apps

    if apps.is_installed("django.contrib.sites"):
        from django.contrib.sites.models import Site

        domain = Site.objects.get_current().domain
        scheme = "https" if getattr(settings, "SITE_USE_HTTPS", False) else "http"
        return f"{scheme}://{domain}"
    return "http://localhost"


def absolute_url(request, path: str) -> str:
    """拼出邮件里可点的绝对地址。"""
    return f"{site_base_url(request)}{path}"


def _clip(text: str, limit: int = EMAIL_TEXT_LIMIT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_reminder_email(notification: InAppNotification, request=None) -> tuple[str, str]:
    """组装提醒邮件，返回 (主题, 正文)。

    主题形如「【即将到期】截止前 2 小时：程序设计竞赛报名」：
    前缀是级别（已过期 / 即将到期 / 提醒），中间是提醒标签，
    这样在收件箱列表里不点开就能判断「哪条通知、还剩多久」。
    """
    prefix = {
        InAppNotification.Level.OVERDUE: "【已过期】",
        InAppNotification.Level.WARN: "【即将到期】",
        InAppNotification.Level.INFO: "【提醒】",
    }.get(notification.level, "【提醒】")
    event = notification.event
    label = (notification.reminder.label if notification.reminder else "") or ""
    title = event.title if event else notification.title
    subject = f"{prefix}{label}：{title}" if label else f"{prefix}{title}"

    lines = ["你设置的事项提醒到点了。", ""]
    if notification.body:
        for part in notification.body.split("　"):
            if part.strip():
                lines.append(part.strip())
    if event is not None:
        # 要求/材料存在 EventField 里（不是 Event 的列），取当前生效值
        requirement = event.field_value("requirement")
        if requirement:
            lines.append(f"要求：{_clip(requirement)}")
        if event.organizer:
            lines.append(f"发布方：{_clip(event.organizer, 80)}")
        if event.link:
            lines.append(f"相关链接：{event.link}")
        lines += ["", f"事项详情：{absolute_url(request, reverse('events:detail', args=[event.pk]))}"]

    lines += [
        "",
        FOOTER_SEPARATOR,
        "这封邮件由「校园通知助手」按你设置的提醒发出。",
        "在「提醒」页可以修改或关闭这条提醒，也可以在「账号」页一次性关闭邮件提醒"
        f"（{absolute_url(request, reverse('accounts:profile'))}）。",
    ]
    return subject, "\n".join(lines)


def send_reminder_email(notification: InAppNotification, request=None) -> bool:
    """给一条已派发的通知发邮件。成功返回 True。

    失败只记日志并返回 False：站内通知照常可见，`email_sent_at` 留空，
    下次派发会重试这条通知，不会因为 SMTP 抖一下就把提醒吞掉。
    """
    event = notification.event
    to = recipient_for(notification.owner)
    if not to:
        logger.info("用户 %s 没有可用邮箱，跳过提醒邮件", notification.owner_id)
        return False

    subject, body = build_reminder_email(notification, request=request)
    if event is not None:
        body += f"\n事项编号：#{event.pk}\n"
    try:
        send_mail(
            subject,
            body,
            getattr(settings, "EMAIL_REMINDER_FROM", "")
            or getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
            [to],
            fail_silently=False,
        )
    except Exception:  # SMTP 超时 / 认证失败 / 网络不通
        logger.exception("提醒邮件发送失败：notification=%s to=%s", notification.pk, to)
        return False

    notification.email_sent_at = timezone.now()
    notification.save(update_fields=["email_sent_at"])
    logger.info("提醒邮件已发送：notification=%s to=%s", notification.pk, to)
    return True


def _mail_pending(notification: InAppNotification, request) -> bool:
    """决定这条通知是否要发邮件；返回是否真的发出去了。

    已经发过、总开关关闭、用户偏好关闭 —— 三种情况都不发。
    偏好读不出来（DB 抖动等）时按「愿意收」处理：宁可多送一封，也不静默丢提醒。
    """
    if not email_enabled() or notification.email_sent_at is not None:
        return False
    try:
        if not user_wants_email(notification.owner):
            return False
    except Exception:  # pragma: no cover
        logger.warning("读取邮件提醒偏好失败，按默认发送处理", exc_info=True)
    return send_reminder_email(notification, request=request)

