"""更正 / 补充 / 重复的话语标记。

这些标记是「同一事件的多条消息」最便宜也最可靠的信号：
一条以「更正：」开头的消息几乎必然指向刚发布的那条通知，而不是新事件。
"""
from __future__ import annotations

from extraction.fields import (
    RELATION_CORRECTION,
    RELATION_DUPLICATE,
    RELATION_SUPPLEMENT,
)

# 明确变更既有信息的表述
CORRECTION_MARKERS = (
    "更正", "更改为", "以此为准", "以本通知为准", "以最新通知为准", "改为", "变更为",
    "调整到", "调整为", "改到", "改至", "延期", "延后", "推后", "顺延", "提前到", "取消",
    "修订", "更新", "最新通知", "重新通知", "地点变更", "时间变更",
)
# 追加信息但不变更既有结论
SUPPLEMENT_MARKERS = (
    "补充", "追加", "另外", "另：", "另:", "同时", "温馨提示", "注意事项",
)
# 取消类标记：合并到既有事件时用于停用提醒
CANCEL_MARKERS = ("取消", "作废", "不再举行", "暂停", "终止")


def has_correction_marker(text: str) -> bool:
    return any(marker in (text or "") for marker in CORRECTION_MARKERS)


def has_supplement_marker(text: str) -> bool:
    return any(marker in (text or "") for marker in SUPPLEMENT_MARKERS)


def has_cancel_marker(text: str) -> bool:
    return any(marker in (text or "") for marker in CANCEL_MARKERS)


def pick_relation(text: str, *, title_identical: bool = False) -> str:
    if has_correction_marker(text):
        return RELATION_CORRECTION
    if has_supplement_marker(text):
        return RELATION_SUPPLEMENT
    if title_identical:
        return RELATION_DUPLICATE
    return RELATION_SUPPLEMENT
