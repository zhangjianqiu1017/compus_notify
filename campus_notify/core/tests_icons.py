"""图标库的用例：名字对得上、渲染得出来、写错了不会炸页面。"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.template import Context, Template
from django.test import SimpleTestCase

from core.templatetags.icons import ICONS

# 模板里 {% icon "name" %} / {% icon "name" 13 %} 的用法
_USAGE = re.compile(r'{%\s*icon\s+"([A-Za-z0-9_-]+)"')


class IconRegistryTests(SimpleTestCase):
    def test_every_icon_used_in_templates_exists(self) -> None:
        """模板里写的图标名必须都在 ICONS 里。

        写错名字时模板会安静地渲染成空字符串（不让整页 500），代价是按钮上悄悄少了
        一个图标；这条用例负责把这种手误在测试阶段就抓出来。
        """
        templates_dir = Path(settings.BASE_DIR) / "templates"
        used: dict[str, set[str]] = {}
        for path in templates_dir.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for name in _USAGE.findall(text):
                used.setdefault(name, set()).add(path.name)

        missing = {name: files for name, files in used.items() if name not in ICONS}
        self.assertEqual(missing, {}, f"模板里用了不存在的图标：{missing}")
        self.assertTrue(used, "没在模板里找到任何 {% icon %} 用法，正则或目录可能变了")

    def test_every_icon_has_paths(self) -> None:
        for name, body in ICONS.items():
            with self.subTest(icon=name):
                self.assertIn("<", body, f"{name} 的图标内容不像 SVG 路径")
                self.assertNotIn("<svg", body, f"{name} 不该自带 <svg> 外壳")


class IconTagTests(SimpleTestCase):
    def render(self, source: str) -> str:
        return Template("{% load icons %}" + source).render(Context({}))

    def test_renders_inline_svg_with_size(self) -> None:
        html = self.render('{% icon "trash" %}')
        self.assertIn("<svg", html)
        self.assertIn('width="15"', html)
        self.assertIn('stroke="currentColor"', html)
        self.assertIn('aria-hidden="true"', html)

    def test_size_and_stroke_can_be_overridden(self) -> None:
        html = self.render('{% icon "bell" 22 1.9 %}')
        self.assertIn('width="22"', html)
        self.assertIn('height="22"', html)
        self.assertIn('stroke-width="1.9"', html)

    def test_unknown_name_renders_nothing(self) -> None:
        self.assertEqual(self.render('{% icon "no-such-icon" %}').strip(), "")

    def test_bad_size_falls_back_to_default(self) -> None:
        html = self.render('{% icon "check" "big" %}')
        self.assertIn('width="15"', html)
