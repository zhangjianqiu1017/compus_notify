"""统一图标库：模板里 `{% icon "trash" %}` 出一个 24×24 线性图标。

为什么要集中一处：

* 同一个语义（导入 / 删除 / 编辑）在全站必须是同一个形状。以前每个模板各画一遍
  SVG，改一个按钮就得记住另外十几处的路径是不是一样的；
* 尺寸、线宽、端点样式只在这里定，模板里只写「这里该出现什么图标」；
* 加一个新图标 = 在 ICONS 里加一行路径，全站立刻可用；写错的图标名不会悄悄画出
  空白图形，而是渲染成空字符串（配合 test_icons.py 的用例兜底）。

风格约定（和顶部导航一致）：viewBox 0 0 24 24、fill:none、stroke:currentColor、
stroke-width 1.8、圆角端点——颜色永远跟随所在按钮/文字的 `color`，深色主题、
危险按钮、次级按钮都不用单独配色。
"""
from __future__ import annotations

from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# name -> SVG 内部路径（不含外层 <svg>，尺寸/线宽由标签统一加）
ICONS: dict[str, str] = {
    # ---------------------------------------------------------- 导航入口
    "home": (
        '<path d="M3 10.5 12 3l9 7.5"/>'
        '<path d="M5.5 9.5V20a1 1 0 0 0 1 1h11a1 1 0 0 0 1-1V9.5"/>'
        '<path d="M10 21v-6h4v6"/>'
    ),
    "import": (
        '<path d="M12 15V3.5"/><path d="M7.5 8 12 3.5 16.5 8"/>'
        '<path d="M4 15v3.5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V15"/>'
    ),
    "download": (
        '<path d="M12 3.5V15"/><path d="M7.5 10.5 12 15l4.5-4.5"/>'
        '<path d="M4 15v3.5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V15"/>'
    ),
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
    "filter": '<path d="M4 5.5h16l-6.2 7.4V19l-3.6-2v-4.1Z"/>',
    "checklist": (
        '<path d="m3 7 2 2 4-4"/><path d="m3 17 2 2 4-4"/>'
        '<path d="M13 7h8"/><path d="M13 17h8"/>'
    ),
    "calendar": (
        '<rect x="3" y="4.5" width="18" height="16.5" rx="2.5"/>'
        '<path d="M8 2.5v4M16 2.5v4M3 10h18"/>'
    ),
    "list": (
        '<path d="M9 6h12M9 12h12M9 18h12"/>'
        '<path d="M4 6h.01M4 12h.01M4 18h.01"/>'
    ),
    # ---------------------------------------------------------- 通知 / 账号
    "bell": (
        '<path d="M18 9a6 6 0 1 0-12 0c0 5-2 6-2 6h16s-2-1-2-6"/>'
        '<path d="M10.5 20a2 2 0 0 0 3 0"/>'
    ),
    "bell-off": (
        '<path d="M18 9a6 6 0 0 0-9.4-4.9"/><path d="M6 9v.5c0 5-2 5.5-2 5.5h12"/>'
        '<path d="M10.5 20a2 2 0 0 0 3 0"/><path d="m3.5 3.5 17 17"/>'
    ),
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
    "settings": (
        '<path d="M4 7.5h9M19 7.5h1M4 16.5h3M13 16.5h7"/>'
        '<circle cx="16" cy="7.5" r="2.4"/><circle cx="10" cy="16.5" r="2.4"/>'
    ),
    "user-plus": (
        '<circle cx="10" cy="8" r="4"/>'
        '<path d="M3 20.5a7 7 0 0 1 11.6-5.4"/>'
        '<path d="M18.5 13.5v6M15.5 16.5h6"/>'
    ),
    "login": (
        '<path d="M14 3.5h3.5a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H14"/>'
        '<path d="M4 12h9.5"/><path d="m10.5 8.5 3.5 3.5-3.5 3.5"/>'
    ),
    "logout": (
        '<path d="M15 3.5h3.5a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H15"/>'
        '<path d="M10 8.5 13.5 12 10 15.5"/><path d="M13.5 12H3"/>'
    ),
    "mail": (
        '<rect x="3" y="5" width="18" height="14" rx="2.5"/>'
        '<path d="m4.2 7.4 7.8 5.2 7.8-5.2"/>'
    ),
    "key": (
        '<circle cx="8" cy="15.5" r="3.8"/><path d="m10.8 12.8 8.7-8.3"/>'
        '<path d="m15.6 7.6 2.2 2.2"/><path d="m18.2 5.1 2 2"/>'
    ),
    "send": (
        '<path d="M20.5 3.5 3 10.5l7 2.5 2.5 7Z"/>'
        '<path d="m10 13 4.6-4.6"/>'
    ),
    "shield": (
        '<path d="M12 3.5 5 6.2v5.3c0 4.4 2.9 7.6 7 9 4.1-1.4 7-4.6 7-9V6.2Z"/>'
        '<path d="m8.9 12 2.3 2.3 4-4.5"/>'
    ),
    "database": (
        '<ellipse cx="12" cy="6" rx="7.5" ry="2.8"/>'
        '<path d="M4.5 6v6c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8V6"/>'
        '<path d="M4.5 12v6c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8v-6"/>'
    ),
    # ---------------------------------------------------------- 操作
    "plus": '<path d="M12 5.5v13M5.5 12h13"/>',
    "check": '<path d="m4.8 12.5 5 5 9.6-10.6"/>',
    "check-circle": (
        '<circle cx="12" cy="12" r="8.5"/><path d="m8.2 12.2 2.8 2.8 5.2-5.6"/>'
    ),
    "undo": (
        '<path d="M3.5 9.5h10a5.5 5.5 0 0 1 0 11H9"/>'
        '<path d="M7.5 5.5 3.5 9.5l4 4"/>'
    ),
    "edit": (
        '<path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17Z"/>'
        '<path d="m14.8 6.2 3 3"/>'
    ),
    "trash": (
        '<path d="M4.5 7h15"/>'
        '<path d="M9.5 7V4.9a1.1 1.1 0 0 1 1.1-1.1h2.8a1.1 1.1 0 0 1 1.1 1.1V7"/>'
        '<path d="M6.6 7l.8 12.1a1.8 1.8 0 0 0 1.8 1.7h5.6a1.8 1.8 0 0 0 1.8-1.7L17.4 7"/>'
        '<path d="M10.4 11v6M13.6 11v6"/>'
    ),
    "x": '<path d="m6 6 12 12M18 6 6 18"/>',
    "alert": (
        '<path d="M12 4.2 21 19.8H3Z"/><path d="M12 10v4.2"/>'
        '<path d="M12 17.1h.01"/>'
    ),
    "refresh": (
        '<path d="M19.5 12a7.5 7.5 0 1 1-2.5-5.6"/>'
        '<path d="M19.8 4.5v4.7h-4.7"/>'
    ),
    "eye": (
        '<path d="M2.5 12S6.2 5.8 12 5.8 21.5 12 21.5 12 17.8 18.2 12 18.2 2.5 12 2.5 12Z"/>'
        '<circle cx="12" cy="12" r="2.8"/>'
    ),
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.2V12l3.2 2"/>',
    "chevron-left": '<path d="m14.5 5.5-7 6.5 7 6.5"/>',
    "chevron-right": '<path d="m9.5 5.5 7 6.5-7 6.5"/>',
    "arrow-left": '<path d="M20 12H4.5"/><path d="M10 6 4 12l6 6"/>',
    "arrow-right": '<path d="M4 12h15.5"/><path d="M14 6l6 6-6 6"/>',
    # ---------------------------------------------------------- 内容语义
    "file-text": (
        '<path d="M13.5 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9Z"/>'
        '<path d="M13.5 3.5V9H19"/><path d="M8.5 13h7M8.5 16.5h4.5"/>'
    ),
    "clipboard": (
        '<path d="M9 4.5h6"/>'
        '<path d="M9 4.5H6.8A1.8 1.8 0 0 0 5 6.3v12.4a1.8 1.8 0 0 0 1.8 1.8h10.4a1.8 1.8 0 0 0 '
        '1.8-1.8V6.3a1.8 1.8 0 0 0-1.8-1.8H15"/>'
        '<path d="M8.6 11h6.8"/><path d="M8.6 15h4.2"/>'
    ),
    "history": (
        '<path d="M3.5 12a8.5 8.5 0 1 0 2.8-6.3"/>'
        '<path d="M3.5 4.5v4.7h4.7"/><path d="M12 8v4.4l3 1.8"/>'
    ),
    "link": (
        '<path d="M10.2 13.8a4 4 0 0 0 5.6 0l2.8-2.8a4 4 0 0 0-5.6-5.6l-1.4 1.4"/>'
        '<path d="M13.8 10.2a4 4 0 0 0-5.6 0l-2.8 2.8a4 4 0 0 0 5.6 5.6l1.4-1.4"/>'
    ),
    "location": (
        '<path d="M12 21s6.5-5.9 6.5-11a6.5 6.5 0 1 0-13 0C5.5 15.1 12 21 12 21Z"/>'
        '<circle cx="12" cy="9.8" r="2.3"/>'
    ),
    "book": (
        '<path d="M4.5 5.5h5.7a2.9 2.9 0 0 1 2.9 2.9V20a2.4 2.4 0 0 0-2.4-2.4H4.5Z"/>'
        '<path d="M19.5 5.5h-5.7a2.9 2.9 0 0 0-2.9 2.9V20a2.4 2.4 0 0 1 2.4-2.4h6.2Z"/>'
    ),
    "flag": '<path d="M6 21V3.5"/><path d="M6 4.5h11l-2.3 4.5L17 13.5H6Z"/>',
    "split": (
        '<circle cx="7" cy="18" r="2.4"/><circle cx="7" cy="6" r="2.4"/>'
        '<path d="M9 7.5 19.5 18M9 16.5 19.5 6"/>'
    ),
    "chat": (
        '<path d="M20.5 12a8.4 8.4 0 0 1-8.5 8.4 9.2 9.2 0 0 1-3.9-.9L4 20.5l1.4-3.7A8.4 8.4 0 1 1 20.5 12Z"/>'
    ),
    "merge": (
        '<path d="M6 3.5v5.7a5 5 0 0 0 5 5h7"/>'
        '<path d="M6 20.5v-5.7a5 5 0 0 1 5-5h7"/>'
        '<path d="m15 11.2 3 3-3 3"/>'
    ),
    "sparkles": (
        '<path d="m11 3.5 1.9 4.8 4.8 1.9-4.8 1.9L11 16.9 9.1 12.1 4.3 10.2l4.8-1.9Z"/>'
        '<path d="m18.2 15.4.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8Z"/>'
    ),
}

_STROKE_DEFAULT = "1.8"


@register.simple_tag
def icon(name: str, size: int = 15, stroke: str = _STROKE_DEFAULT) -> str:
    """渲染一个图标。`{% icon "trash" %}` / `{% icon "bell" 13 %}`。

    name 不存在时返回空字符串：宁可少一个图标，也不要让整页 500。
    """
    body = ICONS.get(str(name))
    if not body:
        return ""
    try:
        px = int(size)
    except (TypeError, ValueError):
        px = 15
    try:
        width = f"{float(stroke):g}"
    except (TypeError, ValueError):
        width = _STROKE_DEFAULT
    return mark_safe(
        f'<svg class="icon" width="{px}" height="{px}" viewBox="0 0 24 24" fill="none"'
        f' stroke="currentColor" stroke-width="{width}" stroke-linecap="round"'
        f' stroke-linejoin="round" aria-hidden="true" focusable="false">{body}</svg>'
    )
