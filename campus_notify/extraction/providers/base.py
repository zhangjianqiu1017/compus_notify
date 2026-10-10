"""抽取提供方（Provider）抽象层。

对应需求 5.5-3：OCR、NLP、推送采用独立接口，方便替换第三方服务。
本模块只依赖 dataclass，不触碰数据库，便于单测与替换。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime

from extraction.fields import (
    CATEGORY_KEYWORDS,
    CATEGORY_OTHER,
    FIELD_CHOICES,
    RELATION_PRIMARY,
)

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class FieldValue:
    """一个候选字段值，始终携带证据（span）与来源消息序号。"""

    field: str
    value: str
    confidence: float = 0.6
    span: tuple[int, int] | None = None
    message_index: int = 1
    resolved_dt: datetime | None = None
    has_time: bool = False


@dataclass
class MessageRelation:
    index: int
    relation: str = RELATION_PRIMARY


@dataclass
class ExtractedEvent:
    title: str
    category: str = CATEGORY_OTHER
    confidence: float = 0.6
    fields: list[FieldValue] = dc_field(default_factory=list)
    messages: list[MessageRelation] = dc_field(default_factory=list)
    raw: dict = dc_field(default_factory=dict)

    def get(self, name: str) -> FieldValue | None:
        for item in self.fields:
            if item.field == name:
                return item
        return None

    def all(self, name: str) -> list[FieldValue]:
        return [item for item in self.fields if item.field == name]


@dataclass
class ExtractionOutcome:
    events: list[ExtractedEvent]
    provider: str
    ignored: list[dict] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)
    duration_ms: int = 0

    @property
    def has_events(self) -> bool:
        return bool(self.events)


# ---------------------------------------------------------------------------
# 共用工具
# ---------------------------------------------------------------------------

VALID_FIELDS = {name for name, _label in FIELD_CHOICES}

# 纯应答/寒暄类消息，直接判为无事件，保护 NFR-01 的 3 秒指标
_NOISE_PATTERNS = [
    re.compile(r"^(收到|好的|好滴|好嘞|谢谢|多谢|感谢|辛苦了|明白|了解|嗯+|哦+|哈+|在吗|有人吗|打卡|签到|ok|OK|Ok|1|11|111)[!！。.~～\s]*$"),
    re.compile(r"^\[?(图片|表情|动画表情|语音|视频|文件|链接)\]?$"),
    re.compile(r"^(撤回了一条消息|.*邀请.*加入了群聊|.*加入了群聊)$"),
]
_TIME_HINT = re.compile(r"\d|[一二三四五六七八九十]{1,3}\s*(?:月|日|号|点|周)|今天|明天|后天|截止|报名")
_ACTION_HINT = re.compile(r"截止|报名|提交|考试|作业|活动|讲座|会议|比赛|竞赛|缴费|领取|选课|调课|集合|签到|开会")


def is_noise(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < 4:
        return True
    for pattern in _NOISE_PATTERNS:
        if pattern.match(stripped):
            return True
    if not _TIME_HINT.search(stripped) and not _ACTION_HINT.search(stripped):
        return True
    return False


def guess_category(text: str) -> str:
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return CATEGORY_OTHER


def clean_title(line: str) -> str:
    """去掉序号、装饰符号，得到可读的事件名。"""
    title = (line or "").strip()
    title = re.sub(r"^[\s\d]*[、.．)）:：\-—*·]+", "", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip()[:80]


# ---------------------------------------------------------------------------
# Provider 接口
# ---------------------------------------------------------------------------


class ExtractionProvider:
    name = "base"
    is_llm = False
    supports_recheck = False

    def extract(self, messages, *, batch=None) -> ExtractionOutcome:  # pragma: no cover - 抽象方法
        raise NotImplementedError

    def recheck_merge(self, existing: dict, draft: dict) -> tuple[bool | None, str]:
        """灰区复核：返回 (是否同一事件, 理由)。None 表示该 Provider 不支持。"""
        return None, ""

    def describe(self) -> str:
        return self.name
