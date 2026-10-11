"""模板辅助标签：原文就地证据标注、置信度分档等。"""
from __future__ import annotations

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from extraction.fields import FIELD_LABELS

register = template.Library()


@register.filter
def mark_evidence(text, annotations) -> str:
    """在原文里就地标注字段证据：<mark>证据片段</mark> + 字段名小标签。

    annotations 形如 [{"start": int, "end": int, "label": "截止时间"}, ...]。
    这样「识别依据」不必再把整段原文按字段重复一遍——用户读原文时就看到了
    「哪个值来自哪一句」。

    重叠时**更精确（更短）的片段胜出**：规则抽取器把「要求/材料」这类整行文本整体
    当作证据，它的跨度会盖住同一条消息里的「截止时间」「链接」。粗跨度让位，
    否则原文里只剩一个盖住整行的高亮，等于没标。
    """
    if not text:
        return ""

    items: list[tuple[int, int, str]] = []
    for item in annotations or []:
        try:
            start = int(item["start"])
            end = int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        if end > start:
            items.append((start, end, str(item.get("label") or "")))
    if not items:
        return escape(text)

    kept: list[tuple[int, int, str]] = []
    for start, end, label in sorted(items, key=lambda row: (row[1] - row[0], row[0])):
        if any(start < kept_end and kept_start < end for kept_start, kept_end, _ in kept):
            continue
        kept.append((start, end, label))
    if not kept:
        return escape(text)
    kept.sort(key=lambda row: row[0])

    out: list[str] = []
    position = 0
    for start, end, label in kept:
        out.append(escape(text[position:start]))
        out.append(f"<mark>{escape(text[start:end])}</mark>")
        if label:
            out.append(f'<span class="ev-label">{escape(label)}</span>')
        position = end
    out.append(escape(text[position:]))
    return mark_safe("".join(out))


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
