"""字段级冲突消解。

规则（顺序即优先级）：
1. 新消息含「更正 / 改为 / 延期」等明确变更表述 → 新值胜出；
2. 发送时间更晚的消息 → 新值胜出；
3. 时间不可比时，置信度高出 0.15 以上 → 新值胜出；
4. 其余保留原值。

被覆盖的值不删除，写入 EventField 历史并由 superseded_by 串联，
这样「报名截止 10/15 改为 10/20」可以既得到唯一当前值，又保留完整审计链（US-06）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from extraction.fields import MULTI_VALUE_FIELDS


@dataclass
class IncomingField:
    """来自抽取结果的一个字段值（与具体模型解耦，方便单测）。"""

    field: str
    value_text: str = ""
    value_dt: datetime | None = None
    confidence: float = 0.6
    sent_at: datetime | None = None
    evidence_span: str = ""
    source_message: object | None = None
    is_correction: bool = False


@dataclass
class IngestPayload:
    """一次待落库的抽取结果。"""

    title: str
    category: str
    confidence: float
    fields: list[IncomingField]
    message_links: list[tuple[object, str]]
    raw_text: str = ""

    @property
    def primary_message(self):
        for message, _relation in self.message_links:
            return message
        return None


def is_multi_value(field_name: str) -> bool:
    return field_name in MULTI_VALUE_FIELDS


def should_replace(existing, incoming: IncomingField) -> tuple[bool, str]:
    """判断新值是否应覆盖既有当前值。existing 为 None 表示此前没有该字段。"""
    if existing is None:
        return True, "首次识别到该字段"
    if incoming.is_correction:
        return True, "新通知含明确更正/变更表述"
    old_sent = getattr(existing, "valid_from", None)
    if incoming.sent_at and old_sent and incoming.sent_at > old_sent:
        return True, "新通知发送时间更晚"
    if (not incoming.sent_at or not old_sent) and incoming.confidence >= (existing.confidence or 0) + 0.15:
        return True, "新值置信度明显更高"
    return False, "保留原值（未发现明确变更依据）"


def merge_multi_value(existing_text: str, new_text: str) -> str:
    """累加型字段（要求/链接/联系人）去重合并，避免后到的补充把前文冲掉。"""
    def split(value: str) -> list[str]:
        parts: list[str] = []
        for chunk in str(value or "").replace("\n", "；").split("；"):
            for piece in chunk.split(";"):
                cleaned = piece.strip()
                if cleaned and cleaned not in parts:
                    parts.append(cleaned)
        return parts

    merged = split(existing_text)
    for item in split(new_text):
        if item not in merged:
            merged.append(item)
    return "；".join(merged)[:400]


def value_of(field_obj) -> str:
    if field_obj is None:
        return ""
    if getattr(field_obj, "value_dt", None):
        return field_obj.value_dt.isoformat()
    return field_obj.value_text or ""
