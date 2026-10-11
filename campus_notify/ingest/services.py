"""导入侧纯函数：消息切分、敏感信息扫描、批次入库。

设计原则（对应讨论确定的信息处理流程 [0] 采集与规范化）：
* 保留消息边界与发送时间——这是后续归并与相对时间锚定的前提；
* 只有真实边界才切分（发送者/时间戳行、「李四：」前缀）；**空行不是边界**——
  用户复制的一段文字本身就是一条原文，不能拆碎成「原始通知 + 补充信息」；
* 切分拿不准时不硬切，退化为「整段一条消息」，交给抽取模型在同一语义任务里拆分；
* 原文去重使用 (batch, message_hash) 唯一约束，保证重复导入幂等。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from ingest.models import Batch, Message

# 关闭 Unicode 宽度差异干扰：全角空格、零宽字符
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\ufeff]")

# 形如「张三 2026-10-08 09:12」或「2026-10-08 09:12 张三」
_HEADER_SENDER_FIRST = re.compile(
    r"^(?P<sender>[^\s:：]{1,20})\s+(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
    r"(?:\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?))?$"
)
_HEADER_TIME_FIRST = re.compile(
    r"^(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
    r"(?:\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?))?(?:\s+(?P<sender>[^\s:：]{1,20}))?$"
)
_HEADER_TIME_ONLY = re.compile(r"^(?P<time>\d{1,2}:\d{2}(?::\d{2})?)$")
_INLINE_SENDER = re.compile(r"^(?P<sender>[^\s:：]{1,16})\s*[:：]\s*(?P<rest>\S.*)$")

# 出现在「XX：」前缀里的这些词几乎一定是字段标签，而不是发言人
_FIELD_KEYWORDS = (
    "时间", "地点", "日期", "费用", "链接", "网址", "截止", "要求", "对象", "内容", "备注",
    "说明", "联系", "主办", "承办", "协办", "形式", "主题", "活动", "课程", "考试", "报名",
    "方式", "平台", "流程", "材料", "标准", "范围", "条件", "注意", "提示", "结果", "名单",
    "安排", "详情", "信息", "地址", "电话", "邮箱", "微信", "群号", "学号", "姓名", "班级",
    "专业", "年级", "名额", "资格", "培训", "讲座", "会议", "初赛", "复赛", "决赛", "签到",
    "集合", "须知", "面向", "人数", "奖励", "议程", "日程", "地点为", "时间为",
)

# 这些词开头的「XX：」是正文标签而不是发送者，避免把通知字段误判成发言人
_INLINE_SENDER_STOPWORDS = {
    "注意", "提示", "温馨提示", "备注", "说明", "补充", "更正", "更新", "修正", "修订",
    "时间", "日期", "地点", "地址", "报名", "费用", "对象", "内容", "要求", "链接", "网址",
    "联系人", "主办", "承办", "协办", "形式", "主题", "截止", "活动", "请", "特别",
    "公众号", "来源", "原文", "紧急", "重要", "特此", "另外", "另", "ps", "PS",
}

_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("学号", re.compile(r"(?:学号|学籍号)[：:\s]*\d{6,14}")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
]


@dataclass
class ParsedMessage:
    """切分结果。sent_at 为 None 表示原文未携带时间戳。"""

    raw_text: str
    sender: str = ""
    sent_at: datetime | None = None
    source_label: str = ""
    extra: dict = field(default_factory=dict)


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    text = _ZERO_WIDTH.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    # 折叠 3 行以上空行，保留段落结构
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            out.append(line)
        else:
            blank_run += 1
            if blank_run <= 1:
                out.append("")
    return "\n".join(out).strip()


def message_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _parse_sent_at(date_str: str | None, time_str: str | None) -> datetime | None:
    if not date_str:
        return None
    date_part = re.split(r"[-/.]", date_str)
    try:
        year, month, day = (int(part) for part in date_part)
    except ValueError:
        return None
    hour, minute = 0, 0
    if time_str:
        bits = time_str.split(":")
        hour, minute = int(bits[0]), int(bits[1])
    naive = datetime(year, month, day, hour, minute)
    if timezone.is_naive(naive):
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return naive


def _is_sender_like(token: str, text: str) -> bool:
    """判断「XX：」里的 XX 到底是发言人还是正文标签。

    通知正文里充斥「地点：」「考试时间：」以及「23:59」这类冒号，
    一旦误判成发言人就会把一条通知切碎，因此这里的门槛必须保守：
    拿不准就不切，整段作为一条消息交给抽取环节处理。
    """
    token = (token or "").strip()
    if not token or len(token) > 16:
        return False
    if token in _INLINE_SENDER_STOPWORDS:
        return False
    if any(keyword in token for keyword in _FIELD_KEYWORDS):
        return False
    if token[-1].isdigit():
        # 「23:59」这种时间表达，冒号前是数字
        return False
    if all("\u4e00" <= char <= "\u9fff" for char in token) and 2 <= len(token) <= 4:
        return True
    # 其余情况要求同一前缀在原文中反复出现（典型群聊记录）
    return text.count(f"{token}：") + text.count(f"{token}:") >= 2


def _match_header(line: str, text: str) -> tuple[str, str, datetime | None, str] | None:
    """返回 (sender, inline_content, sent_at, kind)；None 表示该行不是边界行。"""
    stripped = line.strip()
    if not stripped:
        return None
    for pattern, sender_group in ((_HEADER_SENDER_FIRST, "sender"), (_HEADER_TIME_FIRST, "sender")):
        match = pattern.match(stripped)
        if match:
            sender = (match.groupdict().get(sender_group) or "").strip()
            return sender, "", _parse_sent_at(match.group("date"), match.group("time")), "dated"
    match = _HEADER_TIME_ONLY.match(stripped)
    if match:
        return "", "", None, "time_only"
    match = _INLINE_SENDER.match(stripped)
    if match:
        sender = match.group("sender").strip()
        if _is_sender_like(sender, text):
            return sender, match.group("rest").strip(), None, "inline"
    return None


def split_messages(raw: str, *, source_label: str = "") -> tuple[list[ParsedMessage], bool]:
    """把粘贴内容切成消息列表。

    只按真实边界切分：带发送者/时间戳的群聊记录行，以及「李四：」这类可靠的行内前缀。
    没有边界时整段作为一条原文返回（confident=False）——用户粘贴的一段文字就是一条
    原始信息，不再按空行继续拆碎；其中包含的多个事件由抽取环节在同一语义任务里分辨。
    """
    text = normalize_text(raw)
    if not text:
        return [], True

    lines = text.split("\n")
    messages: list[ParsedMessage] = []
    current: ParsedMessage | None = None
    found_header = False

    def flush() -> None:
        nonlocal current
        if current is not None and current.raw_text.strip():
            current.raw_text = current.raw_text.strip()
            messages.append(current)
        current = None

    for line in lines:
        header = _match_header(line, text)
        if header is None:
            if current is None:
                current = ParsedMessage(raw_text=line, source_label=source_label)
            else:
                current.raw_text += "\n" + line
            continue
        sender, inline, sent_at, _kind = header
        found_header = True
        flush()
        current = ParsedMessage(
            raw_text=inline, sender=sender, sent_at=sent_at, source_label=source_label
        )
    flush()

    if found_header and messages:
        return messages, True

    # 没有边界行：整段就是一条原文。空行只是排版，不是消息边界——
    # 按空行切分会让同一段通知变成「原始通知 + 补充信息」两条原文挂在同一事项上。
    return [ParsedMessage(raw_text=text, source_label=source_label)], False


def scan_sensitive(text: str) -> list[str]:
    """NFR-14：仅做本地扫描与提示，不阻断、不上传额外内容。"""
    kinds: list[str] = []
    for kind, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text or ""):
            kinds.append(kind)
    return kinds


class DuplicateImport(ValueError):
    """整批内容都是此前导入过的原文（幂等去重命中）。

    携带命中的原文明细，便于页面告诉用户「和哪一条撞了、它现在关联了什么事项」，
    而不是只甩一句「完全重复」让人无从下手。
    """

    def __init__(self, message: str, duplicates: list[Message]):
        super().__init__(message)
        self.duplicates = [describe_message(item) for item in duplicates]


def describe_message(message: Message) -> dict:
    """给页面用的原文摘要：何时导入、谁发的、关联了哪些事项。"""
    events = [link.event for link in message.event_links.select_related("event")]
    return {
        "message": message,
        "imported_at": message.batch.created_at if message.batch_id else message.created_at,
        "sender": message.sender,
        "preview": (message.display_text() or "")[:160],
        "events": events,
        "raw_discarded": message.raw_discarded,
    }


def create_batch(
    owner,
    raw_text: str,
    *,
    source: str = Batch.Source.PASTE,
    source_label: str = "",
    note: str = "",
    reuse_existing: bool = False,
) -> tuple[Batch, list[Message], list[str]]:
    """建立批次与消息。返回 (batch, 本批消息, 警告信息)。

    去重规则：同一用户下，原文完全相同（规范化后）的消息只存一份。
    * reuse_existing=False（默认）：命中即跳过，整批都命中则抛 DuplicateImport；
    * reuse_existing=True：复用已有原文重新识别 —— 把这条原文并入本次批次，
      不产生第二份原文副本（既不浪费空间，也避免同一通知生成两条待办）。
    """
    warnings: list[str] = []
    # 没有边界行（用户直接贴通知正文）是最常见的粘贴方式，整段作为一条原文属于正常结果，
    # 不是需要提示的异常：段内多个事项由识别环节拆开，用户无需为此做任何事。
    parsed, _confident = split_messages(raw_text, source_label=source_label)
    if not parsed:
        raise ValueError("导入内容为空")

    limit = int(getattr(settings, "MAX_MESSAGES_PER_BATCH", 40))
    if len(parsed) > limit:
        warnings.append(f"一次导入的消息超过 {limit} 条，仅处理前 {limit} 条。")
        parsed = parsed[:limit]

    batch = Batch.objects.create(owner=owner, source=source, note=note[:200])

    existing_hashes = set(
        Message.objects.filter(owner=owner).values_list("message_hash", flat=True)
    )
    created: list[Message] = []
    duplicates: list[Message] = []
    emptied_batch_ids: set[int] = set()
    seen_in_batch: set[str] = set()

    for index, item in enumerate(parsed, start=1):
        digest = message_hash(item.raw_text)
        if digest in seen_in_batch:
            warnings.append(f"第 {index} 条与本次导入中的其他消息完全相同，已跳过。")
            continue

        if digest in existing_hashes:
            existing = (
                Message.objects.filter(owner=owner, message_hash=digest).order_by("id").first()
            )
            if existing is None:  # pragma: no cover - 理论上不会发生
                continue
            seen_in_batch.add(digest)

            # 这条原文还挂在某个事项上 → 才是真正的「重复导入」，需要用户确认；
            # 若它已经成了孤儿（之前的识别结果被删掉了），复用重新识别没有任何重复风险，
            # 直接放行，不让用户多点一次按钮。
            orphan = not existing.event_links.exists()
            reused = reuse_existing or orphan

            if not reused:
                duplicates.append(existing)
                when = timezone.localtime(
                    existing.batch.created_at if existing.batch_id else existing.created_at
                ).strftime("%m-%d %H:%M")
                warnings.append(
                    f"第 {index} 条已于 {when} 导入过，且仍关联着已有事项，已跳过（如需重新识别请点「仍然重新识别」）。"
                )
                continue
            if orphan and not reuse_existing:
                warnings.append(f"第 {index} 条为已有原文（当前没有对应事项），已直接重新识别。")

            if existing.batch_id:
                emptied_batch_ids.add(existing.batch_id)
            existing.batch = batch
            existing.seq = len(created) + 1
            existing.sent_at = item.sent_at or existing.sent_at
            existing.sender = item.sender[:64] or existing.sender
            existing.source_label = item.source_label[:64] or existing.source_label
            if existing.raw_discarded:
                # 此前按隐私设置丢弃过原文，这里用本次粘贴的内容补回
                existing.raw_text = item.raw_text
                existing.raw_discarded = False
                kinds = scan_sensitive(item.raw_text)
                existing.has_sensitive = bool(kinds)
                existing.sensitive_kinds = ",".join(kinds)[:120]
            existing.save()
            created.append(existing)
            if reuse_existing and not orphan:
                warnings.append(f"第 {index} 条为已有原文，已复用并重新识别（不新建原文副本）。")
            continue

        seen_in_batch.add(digest)
        kinds = scan_sensitive(item.raw_text)
        message = Message.objects.create(
            owner=owner,
            batch=batch,
            seq=len(created) + 1,
            raw_text=item.raw_text,
            sender=item.sender[:64],
            source_label=item.source_label[:64],
            sent_at=item.sent_at,
            message_hash=digest,
            has_sensitive=bool(kinds),
            sensitive_kinds=",".join(kinds)[:120],
        )
        created.append(message)

    # 原文被整体搬到本批次后，源批次可能已经空了，顺手清掉避免留下空壳
    for batch_id in emptied_batch_ids:
        if batch_id == batch.pk:
            continue
        source_batch = Batch.objects.filter(pk=batch_id).annotate(n=Count("messages")).first()
        if source_batch is not None and source_batch.n == 0:
            source_batch.delete()

    if not created:
        batch.delete()
        if duplicates:
            when = timezone.localtime(duplicates[0].created_at).strftime("%m-%d %H:%M")
            raise DuplicateImport(
                f"这段内容在 {when} 已导入过，并且仍关联着下面这些事项，因此没有新建原文。",
                duplicates,
            )
        raise ValueError("导入内容为空，未新建任何消息。")
    return batch, created, warnings


def discard_raw_if_needed(owner, messages: list[Message]) -> int:
    """NFR-15：用户关闭「保存原文」时，识别完成后立即丢弃原文。"""
    from accounts.models import UserPreference

    preference = UserPreference.for_user(owner)
    if preference.store_raw_messages:
        return 0
    updated = 0
    for message in messages:
        if message.raw_discarded:
            continue
        message.raw_text = "[原文已按隐私设置丢弃]"
        message.raw_discarded = True
        message.save(update_fields=["raw_text", "raw_discarded"])
        updated += 1
    return updated


def purge_raw_texts(owner) -> int:
    """FR-14 / NFR-15：删除全部历史原文，保留结构化事项。"""
    return Message.objects.filter(owner=owner, raw_discarded=False).update(
        raw_text="[原文已删除]", raw_discarded=True
    )
