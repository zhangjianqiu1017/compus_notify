"""模板辅助标签：原文证据高亮、置信度分档等。"""
from __future__ import annotations

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from extraction.fields import FIELD_LABELS

register = template.Library()


@register.filter
def highlight_span(text, span) -> str:
    """把证据区间渲染为 <mark>，用于「这条字段值来自原文哪一段」。"""
    if not text:
        return ""
    bounds = None
    if isinstance(span, str) and ":" in span:
        start, _, end = span.partition(":")
        try:
            bounds = (int(start), int(end))
        except ValueError:
            bounds = None
    elif isinstance(span, (tuple, list)) and len(span) == 2:
        bounds = (int(span[0]), int(span[1]))
    if not bounds:
        return escape(text)
    start = max(0, min(bounds[0], len(text)))
    end = max(start, min(bounds[1], len(text)))
    return mark_safe(
        f"{escape(text[:start])}<mark>{escape(text[start:end])}</mark>{escape(text[end:])}"
    )


@register.filter
def field_label(name: str) -> str:
    return FIELD_LABELS.get(name, name)


@register.filter
def confidence_class(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "conf-low"
    if number >= 0.85:
        return "conf-high"
    if number >= 0.6:
        return "conf-mid"
    return "conf-low"


@register.filter
def confidence_percent(value) -> str:
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "—"


@register.filter
def offset_label(minutes) -> str:
    try:
        number = int(minutes)
    except (TypeError, ValueError):
        return str(minutes)
    if number % 1440 == 0:
        return f"{number // 1440} 天前"
    if number % 60 == 0:
        return f"{number // 60} 小时前"
    return f"{number} 分钟前"
