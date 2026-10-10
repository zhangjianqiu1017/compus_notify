"""本地规则抽取器（离线兜底 Provider）。

它不是「玩具」：在没有任何 API Key 的环境下，整个应用必须可用
（对应 5.6「识别服务故障时允许手动新建事项，APP 整体可用」）。
能力边界要说清楚：
* 时间/地点/链接/联系人/课程等强模式字段，规则表现稳定；
* 一条消息里包含多个独立事项时，规则不拆分（只作为一个事件的多个字段），
  这一项依赖大模型 Provider。
"""
from __future__ import annotations

import re
import time as time_module

from django.conf import settings
from django.utils import timezone

from extraction.fields import (
    CATEGORY_OTHER,
    FIELD_CONTACT,
    FIELD_COURSE,
    FIELD_DEADLINE,
    FIELD_END_AT,
    FIELD_LINK,
    FIELD_LOCATION,
    FIELD_ORGANIZER,
    FIELD_REQUIREMENT,
    FIELD_START_AT,
    RELATION_PRIMARY,
)
from extraction.markers import CORRECTION_MARKERS
from extraction.normalize import extract_temporal
from extraction.providers.base import (
    ExtractedEvent,
    ExtractionOutcome,
    ExtractionProvider,
    FieldValue,
    MessageRelation,
    clean_title,
    guess_category,
    is_noise,
)

# 关键词顺序很重要：先判结束，再判截止（「截止」里含「止」），最后判开始。
_END_KEYS = ("结束", "散场", "闭馆", "闭幕")
_DEADLINE_KEYS = ("截止", "截至", "之前", "前完成", "前提交", "前上交", "前报名",
                  "deadline", "最后期限", "过期", "为止")
_START_KEYS = ("开始", "举行", "举办", "进行", "召开", "时间", "于", "当天", "集合", "签到")

_LOCATION_PATTERNS = [
    re.compile(r"(?:地点|地址|会场|会议地点|活动地点|集合地点)\s*[:：]?\s*([^\n，,。；;、]{2,40})"),
    re.compile(r"在\s*([^\n，,。；;、]{2,30}?)\s*(?:举行|举办|开展|进行|召开|集合|签到)"),
    re.compile(
        r"([\u4e00-\u9fa5A-Za-z0-9]{1,18}(?:报告厅|教学楼|实验楼|综合楼|体育馆|图书馆|"
        r"会议室|活动室|大礼堂|操场|教室|机房|[A-Za-z]?\d{0,3}栋?\d{2,4}室))"
    ),
]
_LINK_RE = re.compile(r"https?://[^\s，,。；;）)】\]\"']+")
_COURSE_PATTERNS = [
    re.compile(r"《([^》]{1,30})》"),
    re.compile(r"([\u4e00-\u9fa5]{2,12})(?:课程|大作业|作业|实验|考试|课程设计|论文)"),
]
# 这些词出现在候选课程名里，说明抓到的是公文标题片段而不是课程
_COURSE_REJECT_WORDS = (
    "关于", "通知", "公告", "安排", "期末", "期中", "学年", "秋季", "春季", "大赛", "竞赛",
    "比赛", "活动", "时间", "地点", "内容", "要求", "本次", "以下", "各位", "同学",
)
_CONTACT_RE = re.compile(
    r"(?:联系人|联系|咨询|负责人)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4}|1[3-9]\d{9}|[Qq]{2}\s*[:：]?\s*\d{5,12})"
)
_ORGANIZER_RE = re.compile(r"(?:主办单位|主办方|主办|承办单位|承办|组织单位)\s*[:：]?\s*([^\n，,。；;]{2,30})")
_REQUIREMENT_KEYS = ("要求", "需携带", "携带", "材料", "提交", "务必", "请", "注意")

# 「地点改到实验楼B302」会把动词一起抓进来，需要剥掉前缀
_LOCATION_PREFIX_RE = re.compile(
    r"^(?:不变|仍为|仍设在|改到|改为|改至|调整到|调整为|变更为|变更到|移至|移师|设在|定在|是|在|为|到)\s*[:：]?\s*"
)


def clean_location(value: str) -> str:
    cleaned = (value or "").strip(" ：:，,。.；;")
    while True:
        stripped = _LOCATION_PREFIX_RE.sub("", cleaned).strip(" ：:，,。.；;")
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


class RuleBasedProvider(ExtractionProvider):
    name = "rules"
    is_llm = False
    supports_recheck = False

    def extract(self, messages, *, batch=None) -> ExtractionOutcome:
        started = time_module.monotonic()
        events: list[ExtractedEvent] = []
        ignored: list[dict] = []
        warnings: list[str] = []

        for message in messages:
            text = message.raw_text
            if is_noise(text):
                ignored.append({"index": message.seq, "reason": "未识别到时间或事项关键词"})
                continue
            events.append(self._extract_one(message, text))

        if len([m for m in messages]) > 1:
            warnings.append(
                "当前使用本地规则抽取器：同一条消息内的多个事项不会自动拆分，"
                "跨消息的合并按「更正/补充」字样与相似度判定。"
            )

        duration = int((time_module.monotonic() - started) * 1000)
        return ExtractionOutcome(
            events=events, provider=self.name, ignored=ignored, warnings=warnings, duration_ms=duration
        )

    # ------------------------------------------------------------------
    def _extract_one(self, message, text: str) -> ExtractedEvent:
        anchor = message.anchor_time
        fields: list[FieldValue] = []

        title = self._extract_title(text)
        category = guess_category(text) or CATEGORY_OTHER

        self._extract_times(text, anchor, fields)
        self._extract_location(text, fields)
        self._extract_link(text, fields)
        self._extract_course(text, fields)
        self._extract_contact(text, fields)
        self._extract_organizer(text, fields)
        self._extract_requirements(text, fields)

        confidences = [item.confidence for item in fields] or [0.5]
        confidence = round(sum(confidences) / len(confidences), 2)
        # 标注每个字段来自哪条消息：下游要靠它判断「是否本条为更正」以及用哪条消息的
        # 发送时间做相对时间锚点。漏标会退化成「永远用第一条消息」，造成更正值不生效、
        # 跨天批次里相对时间算错。
        for item in fields:
            item.message_index = message.seq
        return ExtractedEvent(
            title=title,
            category=category,
            confidence=confidence,
            fields=fields,
            messages=[MessageRelation(index=message.seq, relation=RELATION_PRIMARY)],
        )

    @staticmethod
    def _extract_title(text: str) -> str:
        bracket = re.search(r"【([^】]{2,60})】", text)
        if bracket:
            return clean_title(bracket.group(1))
        about = re.search(r"关于([^\n。；;]{2,60}?)(?:的通知|的公告|的说明|通知|公告)", text)
        if about:
            return clean_title(f"关于{about.group(1)}的通知")
        for line in text.split("\n"):
            candidate = clean_title(line)
            if len(candidate) >= 4:
                return candidate
        return clean_title(text)[:40]

    @staticmethod
    def _extract_times(text: str, anchor, fields: list[FieldValue]) -> None:
        hits = extract_temporal(text, anchor)
        if not hits:
            return
        # 「原定10月14日的课调整为10月16日下午2点」：变更词之前的时间描述的是旧安排，
        # 生效的是变更词之后的时间点，否则会把被取消的时间写成当前值。
        marker_positions = [text.find(marker) for marker in CORRECTION_MARKERS if marker in text]
        if marker_positions and len(hits) > 1:
            marker_pos = min(marker_positions)
            after = [hit for hit in hits if hit.span[0] >= marker_pos]
            if after:
                hits = after

        deadline = start = end = None
        leftovers = []
        for hit in hits:
            window = text[max(0, hit.span[0] - 14): hit.span[1] + 14]
            if hit.kind_hint == "range_start":
                start = start or hit
                continue
            if hit.kind_hint == "range_end":
                end = end or hit
                continue
            if any(key in window for key in _END_KEYS):
                end = end or hit
            elif any(key in window for key in _DEADLINE_KEYS):
                deadline = deadline or hit
            elif any(key in window for key in _START_KEYS):
                start = start or hit
            else:
                leftovers.append(hit)

        if deadline is None and start is None and end is None:
            only = hits[0]
            if len(hits) == 1 and ("截止" in text or "前" in text):
                deadline = only
            else:
                start = only
                leftovers = leftovers[1:]

        for hit in leftovers:
            if start is None:
                start = hit
            elif end is None:
                end = hit

        if deadline is not None:
            fields.append(
                FieldValue(
                    field=FIELD_DEADLINE,
                    value=deadline.text,
                    confidence=deadline.confidence,
                    span=deadline.span,
                    resolved_dt=deadline.dt if deadline.has_time else None,
                    has_time=deadline.has_time,
                )
            )
        if start is not None:
            fields.append(
                FieldValue(
                    field=FIELD_START_AT,
                    value=start.text,
                    confidence=start.confidence,
                    span=start.span,
                    resolved_dt=start.dt if start.has_time else None,
                    has_time=start.has_time,
                )
            )
        if end is not None:
            fields.append(
                FieldValue(
                    field=FIELD_END_AT,
                    value=end.text,
                    confidence=end.confidence,
                    span=end.span,
                    resolved_dt=end.dt if end.has_time else None,
                    has_time=end.has_time,
                )
            )

    @staticmethod
    def _first_match(patterns, text: str):
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return match
        return None

    def _extract_location(self, text: str, fields: list[FieldValue]) -> None:
        match = self._first_match(_LOCATION_PATTERNS, text)
        if not match:
            return
        value = clean_location(match.group(1))
        if len(value) < 2:
            return
        # 证据跨度对准清理后的值，而不是包含「不变：」等前缀的整段
        position = text.find(value, match.start())
        span = (position, position + len(value)) if position >= 0 else match.span(1)
        fields.append(FieldValue(field=FIELD_LOCATION, value=value, confidence=0.7, span=span))

    def _extract_link(self, text: str, fields: list[FieldValue]) -> None:
        seen = set()
        for match in _LINK_RE.finditer(text):
            url = match.group(0).rstrip("。，,.;；")
            if url in seen:
                continue
            seen.add(url)
            fields.append(FieldValue(field=FIELD_LINK, value=url, confidence=0.95, span=match.span()))

    def _extract_course(self, text: str, fields: list[FieldValue]) -> None:
        # 「关于软件工程课程调课的通知」：去掉「关于」才能抓到真正的课程名
        stripped = text.replace("关于", "")
        for pattern_index, pattern in enumerate(_COURSE_PATTERNS):
            for match in pattern.finditer(stripped):
                candidate = match.group(1).strip()
                if len(candidate) < 2 or len(candidate) > 12:
                    continue
                if pattern_index == 1 and any(word in candidate for word in _COURSE_REJECT_WORDS):
                    continue
                position = text.find(candidate)
                span = (position, position + len(candidate)) if position >= 0 else None
                fields.append(
                    FieldValue(field=FIELD_COURSE, value=candidate, confidence=0.7, span=span)
                )
                return

    def _extract_contact(self, text: str, fields: list[FieldValue]) -> None:
        match = _CONTACT_RE.search(text)
        if not match:
            return
        fields.append(FieldValue(field=FIELD_CONTACT, value=match.group(1).strip(), confidence=0.75, span=match.span(1)))

    def _extract_organizer(self, text: str, fields: list[FieldValue]) -> None:
        match = _ORGANIZER_RE.search(text)
        if not match:
            return
        fields.append(FieldValue(field=FIELD_ORGANIZER, value=match.group(1).strip(), confidence=0.7, span=match.span(1)))

    def _extract_requirements(self, text: str, fields: list[FieldValue]) -> None:
        sentences = re.split(r"[\n。；;]", text)
        picked = []
        span_start = None
        for sentence in sentences:
            stripped = sentence.strip()
            if len(stripped) < 4:
                continue
            if any(key in stripped for key in _REQUIREMENT_KEYS):
                if len(picked) >= 3:
                    break
                picked.append(stripped)
                if span_start is None:
                    index = text.find(stripped)
                    if index >= 0:
                        span_start = (index, index + len(stripped))
        if not picked:
            return
        fields.append(
            FieldValue(
                field=FIELD_REQUIREMENT,
                value="；".join(picked)[:200],
                confidence=0.6,
                span=span_start,
            )
        )

    def describe(self) -> str:
        return "本地规则抽取器"


def _resolve_anchor(message):
    return getattr(message, "anchor_time", None) or timezone.now()


def available() -> bool:
    """规则抽取器始终可用。"""
    return bool(getattr(settings, "EXTRACTION_PROVIDER", "auto"))
