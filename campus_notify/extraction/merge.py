"""事件归并：指纹、候选召回、相似度打分与三区间决策。

对应讨论确定流程的 [4][5] 步：
* 绝不做全库 LLM 比对（成本与延迟都会崩），先指纹粗筛再规则精排；
* 硬命中（同一报名链接）直接判定，不浪费模型调用；
* 灰区不静默决定 —— 交给用户在确认页拍板（NFR-05：歧义内容禁止静默保存）。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta

from django.conf import settings

from extraction.fields import (
    DATETIME_FIELDS,
    FIELD_COURSE,
    FIELD_DEADLINE,
    FIELD_LINK,
    FIELD_LOCATION,
    FIELD_START_AT,
)
from extraction.markers import has_correction_marker

DECISION_MERGE = "merge"
DECISION_REVIEW = "review"
DECISION_NEW = "new"

# 标题里没有信息量的装饰词，归一化时去掉
_TITLE_NOISE = (
    "关于", "的通知", "通知", "公告", "通告", "温馨提示", "补充说明", "说明", "特此", "如下",
)

_FULLWIDTH_BASE = 0xFEE0


def _to_halfwidth(text: str) -> str:
    out = []
    for char in text:
        code = ord(char)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - _FULLWIDTH_BASE))
        else:
            out.append(char)
    return "".join(out)


def normalize_text(text: str) -> str:
    """全角转半角 + 去空白 + 小写，用于相似度比较。"""
    return re.sub(r"\s+", "", _to_halfwidth(text or "").lower())


def normalize_title(title: str) -> str:
    text = normalize_text(title)
    for noise in _TITLE_NOISE:
        text = text.replace(noise, "")
    text = re.sub(r"[【】\[\]（）()《》<>\"'“”‘’,，。.、:：;；!！?？~～\-—_/\\|*·]+", "", text)
    return text


def char_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def bigram_jaccard(a: str, b: str) -> float:
    if len(a) < 2 or len(b) < 2:
        return 0.0
    grams_a = {a[i: i + 2] for i in range(len(a) - 1)}
    grams_b = {b[i: i + 2] for i in range(len(b) - 1)}
    if not grams_a or not grams_b:
        return 0.0
    return len(grams_a & grams_b) / len(grams_a | grams_b)


@dataclass
class EventSnapshot:
    """用于归并比较的事件画像（不依赖具体模型实例，便于单测）。"""

    title: str = ""
    course: str = ""
    location: str = ""
    url: str = ""
    deadline: datetime | None = None
    start_at: datetime | None = None
    source_key: str = ""
    raw_text: str = ""
    event_id: int | None = None
    title_norm: str = dc_field(default="", init=False)

    def __post_init__(self) -> None:
        self.title_norm = normalize_title(self.title)


def snapshot_from_event(event) -> EventSnapshot:
    return EventSnapshot(
        title=event.title,
        course=event.course or "",
        location=event.location or "",
        url=event.link or "",
        deadline=event.deadline,
        start_at=event.start_at,
        source_key=event.source_key(),
        raw_text="\n".join(link.message.raw_text for link in event.links.select_related("message")),
        event_id=event.pk,
    )


def score_pair(existing: EventSnapshot, incoming: EventSnapshot) -> tuple[float, list[str]]:
    """返回 (0~1 的同一事件可能性, 可解释的理由列表)。"""
    reasons: list[str] = []

    # 1) 硬命中：同一个报名链接 / 原文链接 → 直接认定同一事件
    if existing.url and incoming.url and normalize_text(existing.url) == normalize_text(incoming.url):
        return 0.99, ["报名或原文链接完全相同"]

    score = 0.0

    # 2) 标题
    if existing.title_norm and existing.title_norm == incoming.title_norm:
        score += 0.55
        reasons.append("事件名完全一致")
    else:
        similarity = char_similarity(existing.title_norm, incoming.title_norm)
        if similarity:
            score += 0.45 * similarity
            if similarity >= 0.5:
                reasons.append(f"事件名相似度 {similarity:.2f}")
        jaccard = bigram_jaccard(existing.title_norm, incoming.title_norm)
        if jaccard >= 0.5:
            score += 0.10
            reasons.append(f"关键词重叠度 {jaccard:.2f}")

    # 3) 课程 / 地点 / 来源
    if existing.course and incoming.course and normalize_text(existing.course) == normalize_text(incoming.course):
        score += 0.15
        reasons.append("同一课程")
    if existing.location and incoming.location and normalize_text(existing.location) == normalize_text(incoming.location):
        score += 0.10
        reasons.append("同一地点")
    if existing.source_key and incoming.source_key and existing.source_key == incoming.source_key:
        score += 0.05
        reasons.append("同一来源")

    # 4) 时间邻近
    delta = _time_delta(existing, incoming)
    if delta is not None:
        if delta <= timedelta(hours=12):
            score += 0.25
            reasons.append("时间点几乎一致")
        elif delta <= timedelta(days=3):
            score += 0.15
            reasons.append("时间点接近")
        elif delta > timedelta(days=14):
            score -= 0.25
            reasons.append("时间点相差较大")

    # 5) 「更正 / 补充」字样是强信号
    if has_correction_marker(incoming.raw_text) or has_correction_marker(existing.raw_text):
        score += 0.15
        reasons.append("含更正/变更表述")

    return max(0.0, min(score, 0.98)), reasons


def _time_delta(a: EventSnapshot, b: EventSnapshot) -> timedelta | None:
    for picker in (lambda s: s.deadline, lambda s: s.start_at):
        left, right = picker(a), picker(b)
        if left and right:
            return abs(left - right)
    return None


def primary_anchor(snapshot: EventSnapshot) -> tuple[str | None, datetime | None]:
    """事项的主时间锚点：截止优先，其次开始时间。

    与 reminders.services.anchor_for_event 保持同一口径 —— 一个事项只服务一个锚点，
    所以「锚点不同」就等于「这是两件要分别提醒的事」。
    """
    if snapshot.deadline:
        return FIELD_DEADLINE, snapshot.deadline
    if snapshot.start_at:
        return FIELD_START_AT, snapshot.start_at
    return None, None


def anchor_kind_differs(a: EventSnapshot, b: EventSnapshot) -> bool:
    """两个画像的主锚点是不是「不同角色」：一个是截止、一个是开始。

    这正是「先提交材料、后课堂汇报」的签名。此时合并会把两个阶段挤进一个事项，
    而一个事项只编排一套提醒（截止优先），后一个阶段会彻底丢掉提醒。
    """
    left_kind, _ = primary_anchor(a)
    right_kind, _ = primary_anchor(b)
    if left_kind is None or right_kind is None:
        return False
    return left_kind != right_kind


def anchors_conflict(a: EventSnapshot, b: EventSnapshot) -> bool:
    """锚点是否指向不同阶段：角色不同，或同类锚点取值不同。

    取自同一条原文的多个阶段事项天然共享课程、来源与措辞，
    只靠相似度打分很容易被并回一件，因此同批次内必须先用这条硬规则挡住。
    """
    left_kind, left_dt = primary_anchor(a)
    right_kind, right_dt = primary_anchor(b)
    if left_kind is None or right_kind is None:
        return False
    if left_kind != right_kind:
        return True
    return left_dt != right_dt


def decide(score: float) -> str:
    auto = float(getattr(settings, "MERGE_AUTO_THRESHOLD", 0.86))
    new = float(getattr(settings, "MERGE_NEW_THRESHOLD", 0.55))
    if score >= auto:
        return DECISION_MERGE
    if score < new:
        return DECISION_NEW
    return DECISION_REVIEW


def decide_reason(score: float, reasons: list[str]) -> str:
    auto = float(getattr(settings, "MERGE_AUTO_THRESHOLD", 0.86))
    new = float(getattr(settings, "MERGE_NEW_THRESHOLD", 0.55))
    detail = "、".join(reasons) if reasons else "无显著相似信号"
    if score >= auto:
        return f"自动合并（相似度 {score:.2f} ≥ {auto}）：{detail}"
    if score < new:
        return f"新建事项（相似度 {score:.2f} < {new}）：{detail}"
    return f"疑似与已有事项重复（相似度 {score:.2f} 处于待确认区间）：{detail}"


def recall_candidates(owner, snapshot: EventSnapshot, *, exclude_ids=(), limit: int | None = None):
    """候选召回：先用数据库粗筛（最近更新的一批已确认事项），再规则精排取 top-k。"""
    from events.models import Event

    limit = int(limit or getattr(settings, "MERGE_CANDIDATE_LIMIT", 5))
    queryset = (
        Event.objects.filter(owner=owner, status=Event.Status.CONFIRMED)
        .exclude(id__in=[pk for pk in exclude_ids if pk])
        .order_by("-last_updated_at")[:200]
    )
    scored = []
    for event in queryset:
        score, reasons = score_pair(snapshot_from_event(event), snapshot)
        scored.append((event, score, reasons))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def field_hint_for_snapshot(payload_fields) -> dict:
    """从抽取结果里挑出用于画像的字段值。"""
    hint: dict[str, object] = {}
    for item in payload_fields:
        name = getattr(item, "field", None)
        if name in DATETIME_FIELDS and name not in hint:
            hint[name] = getattr(item, "value_dt", None)
        elif name in (FIELD_COURSE, FIELD_LOCATION, FIELD_LINK) and name not in hint:
            hint[name] = getattr(item, "value_text", "")
    return {
        "course": str(hint.get(FIELD_COURSE) or ""),
        "location": str(hint.get(FIELD_LOCATION) or ""),
        "url": str(hint.get(FIELD_LINK) or ""),
        "deadline": hint.get(FIELD_DEADLINE),
        "start_at": hint.get(FIELD_START_AT),
    }
