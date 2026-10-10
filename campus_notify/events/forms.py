"""事项相关表单：确认门、编辑、手动新建。"""
from __future__ import annotations

from django import forms
from django.conf import settings

from events.models import Event
from extraction.fields import CATEGORY_CHOICES

_DATETIME_WIDGET = forms.TextInput(attrs={"type": "datetime-local", "step": "60"})

TEXT_FIELDS = ("course", "location", "organizer", "link", "contact")


def reminder_option_choices(extra_offsets=()) -> list[tuple[str, str]]:
    """可选提醒点。

    extra_offsets 用于带上该事项已有的自定义提前量：用户在提醒页把提醒改成
    「提前 3 小时」之后，编辑页必须能表示这个值，否则保存时会把它清掉。
    """
    presets = {0, 15, 30, 60, 120, 240, 720, 1440, 2880, 10080}
    presets.update(int(item) for item in (extra_offsets or []))
    for offsets in (getattr(settings, "REMINDER_DEFAULT_OFFSETS", {}) or {}).values():
        presets.update(int(item) for item in offsets)
    choices = []
    for minutes in sorted(presets):
        if minutes == 0:
            label = "到点时提醒"
        elif minutes % 1440 == 0:
            label = f"提前 {minutes // 1440} 天"
        elif minutes % 60 == 0:
            label = f"提前 {minutes // 60} 小时"
        else:
            label = f"提前 {minutes} 分钟"
        choices.append((str(minutes), label))
    return choices


def default_reminder_selection(has_deadline: bool) -> list[str]:
    table = getattr(settings, "REMINDER_DEFAULT_OFFSETS", {}) or {}
    key = "deadline" if has_deadline else "start_at"
    return [str(int(item)) for item in table.get(key, [])]


class EventFormMixin(forms.Form):
    title = forms.CharField(label="事件名", max_length=200)
    category = forms.ChoiceField(label="分类", choices=CATEGORY_CHOICES)
    course = forms.CharField(label="课程", required=False, max_length=120)
    location = forms.CharField(label="地点", required=False, max_length=200)
    organizer = forms.CharField(label="主办方", required=False, max_length=120)
    link = forms.CharField(label="链接", required=False, max_length=500)
    contact = forms.CharField(label="联系人", required=False, max_length=120)
    requirement = forms.CharField(
        label="要求/材料", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    deadline = forms.CharField(label="截止时间", required=False, widget=_DATETIME_WIDGET)
    start_at = forms.CharField(label="开始时间", required=False, widget=_DATETIME_WIDGET)
    end_at = forms.CharField(label="结束时间", required=False, widget=_DATETIME_WIDGET)
    save_as_todo = forms.BooleanField(label="保存为待办", required=False)
    save_as_calendar = forms.BooleanField(label="保存为日程", required=False)
    reminder_offsets = forms.MultipleChoiceField(
        label="提醒时间", required=False, widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, extra_offsets=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reminder_offsets"].choices = reminder_option_choices(extra_offsets)

    def cleaned_values(self) -> dict:
        values = {
            "title": self.cleaned_data.get("title", ""),
            "category": self.cleaned_data.get("category", ""),
        }
        for name in TEXT_FIELDS + ("requirement", "deadline", "start_at", "end_at"):
            values[name] = (self.cleaned_data.get(name) or "").strip()
        return values

    def offsets(self) -> list[int]:
        return [int(item) for item in (self.cleaned_data.get("reminder_offsets") or [])]


class ReviewForm(EventFormMixin):
    """确认门表单（FR-05）：所有字段可改，并决定是否与候选事件合并。"""

    merge_choice = forms.ChoiceField(
        label="重复处理",
        required=False,
        choices=[("keep", "保持为独立事项"), ("merge", "合并到已有事项")],
        widget=forms.RadioSelect,
        initial="keep",
    )


class EventEditForm(EventFormMixin):
    is_cancelled = forms.BooleanField(label="标记为已取消", required=False)


class ManualEventForm(EventFormMixin):
    """识别失败时的降级入口（5.6）：手动新建事项。"""

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("deadline") and not cleaned.get("start_at"):
            raise forms.ValidationError("请至少填写截止时间或开始时间。")
        return cleaned


class QuickFilterForm(forms.Form):
    q = forms.CharField(required=False, label="关键词")
    category = forms.ChoiceField(
        required=False, choices=[("", "全部分类"), *CATEGORY_CHOICES], label="分类"
    )
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "全部状态"),
            (Event.TodoStatus.PENDING, "待完成"),
            (Event.TodoStatus.DONE, "已完成"),
        ],
        label="状态",
    )
