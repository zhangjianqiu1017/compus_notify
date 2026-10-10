"""Provider 选择入口。

EXTRACTION_PROVIDER:
* auto  —— 配置了 LLM_API_KEY 就用大模型，否则本地规则抽取器（默认）
* llm   —— 强制大模型，失败即报错（线上定位问题用）
* rules —— 强制规则抽取器（离线开发 / 单测）
"""
from __future__ import annotations

from django.conf import settings

from extraction.providers.base import (
    ExtractedEvent,
    ExtractionOutcome,
    ExtractionProvider,
    FieldValue,
    MessageRelation,
)
from extraction.providers.llm import ExtractionError, OpenAICompatProvider
from extraction.providers.rules import RuleBasedProvider

__all__ = [
    "ExtractedEvent",
    "ExtractionError",
    "ExtractionOutcome",
    "ExtractionProvider",
    "FieldValue",
    "MessageRelation",
    "OpenAICompatProvider",
    "RuleBasedProvider",
    "get_provider",
]


def get_provider(name: str | None = None) -> ExtractionProvider:
    mode = (name or getattr(settings, "EXTRACTION_PROVIDER", "auto") or "auto").lower()
    if mode == "rules":
        return RuleBasedProvider()
    if mode == "llm":
        return OpenAICompatProvider()
    if getattr(settings, "LLM_API_KEY", ""):
        return OpenAICompatProvider()
    return RuleBasedProvider()
