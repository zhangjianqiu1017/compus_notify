"""确定性时间归一化。

这是整条链路里最容易被低估、也最容易出错的一步，全部用可单测的代码实现：
* 相对时间锚定「消息发送时间」，而不是「当前时间」（用户经常导入几天前的通知）；
* 「10月20日」在 12 月导入时自动推断为下一年；
* 缺时刻时按字段给默认值（作业截止 23:59），绝不静默猜测具体时刻。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from extraction.fields import FIELD_DEADLINE

CN_NUM = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}
RELATIVE_DAYS = {
    "大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明晚": 1, "明早": 1,
    "今天": 0, "今日": 0, "今晚": 0, "今早": 0, "昨天": -1,
}

PRECISION_CONFIDENCE = {
    "ymd": 0.95,
    "md": 0.85,
    "d": 0.60,
    "relative": 0.78,
    "week": 0.72,
    "term": 0.68,
    "dayoffset": 0.75,
    "houroffset": 0.92,
    "time_only": 0.60,
}

# 按优先级顺序匹配；先匹配到的区间会被遮蔽，避免「第7周周三」被拆坏
_DATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ymd", re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]?")),
    ("md", re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]")),
    ("term", re.compile(r"第\s*(?P<n>[一二三四五六七八九十\d]{1,3})\s*周")),
    ("rel", re.compile(r"(?P<rel>大后天|后天|明天|明日|明晚|明早|今晚|今早|今天|今日|昨天)")),
    (
        "week",
        re.compile(
            r"(?<!\d)(?P<prefix>下下|本周|这周|这个星期|下周|下个星期|上周|下个周)?\s*"
            r"(?:周|星期|礼拜)\s*(?P<wd>[一二三四五六日天1-7])"
        ),
    ),
    ("dayoffset", re.compile(r"(?P<n>[0-9一二三四五六七八九十]{1,3})\s*天\s*(?:以?后|内)")),
    ("houroffset", re.compile(r"(?P<n>[0-9一二三四五六七八九十]{1,3})\s*(?:个)?小时\s*(?:以?后|内)")),
    ("d", re.compile(r"(?P<d>\d{1,2})\s*[日号]")),
]

_TIME_RE = re.compile(
    r"(?<!小)(?P<period>凌晨|早上|早晨|上午|中午|正午|下午|傍晚|晚上|夜里)?\s*"
    r"(?P<h>\d{1,2})\s*(?P<sep>[:：点时])\s*(?P<m>\d{1,2})?\s*(?:分)?(?P<half>半)?"
)

_RANGE_RE = re.compile(
    r"(?<!小)(?P<a>\d{1,2}\s*[:：点时]\s*\d{0,2}\s*(?:分)?)\s*"
    r"(?:-|—|–|~|～|至|到)\s*"
    r"(?P<b>\d{1,2}\s*[:：点时]\s*\d{0,2}\s*(?:分)?)"
)


@dataclass(frozen=True)
class TemporalHit:
    """文本中的一处时间表达及其解析结果。"""

    dt: datetime
    span: tuple[int, int]
    text: str
    has_date: bool
    has_time: bool
    precision: str
    kind_hint: str | None = None

    @property
    def confidence(self) -> float:
        return PRECISION_CONFIDENCE.get(self.precision, 0.6)


@dataclass
class _DateCand:
    span: tuple[int, int]
    kind: str
    text: str
    day: date | None = None
    exact: datetime | None = None
    period_hint: str | None = None


def cn_to_int(token: str) -> int | None:
    """支持「七」「十」「十一」「二十三」等中文数字。"""
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if token.startswith("十"):
        rest = CN_NUM.get(token[1:], None)
        return 10 + rest if rest is not None else None
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = CN_NUM.get(head)
        units = CN_NUM.get(tail, 0) if tail else 0
        if tens is None:
            return None
        return tens * 10 + units
    return CN_NUM.get(token)


def term_start_date() -> date:
    raw = getattr(settings, "TERM_START_DATE", "") or ""
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return date(2026, 9, 7)


def default_time_for(field: str | None) -> time | None:
    table = getattr(settings, "DEFAULT_TIME_BY_FIELD", {}) or {}
    raw = table.get(field or "", None)
    if not raw:
        return None
    try:
        hour, _, minute = str(raw).partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return None


def _aware(day: date, moment: time) -> datetime:
    naive = datetime.combine(day, moment)
    tz = timezone.get_current_timezone()
    return timezone.make_aware(naive, tz)


def _infer_year(month: int, day_of_month: int, anchor: datetime) -> date:
    """跨年推断：解析出的日期比锚点早 30 天以上，认为说的是下一年。"""
    try:
        candidate = date(anchor.year, month, day_of_month)
    except ValueError:
        return anchor.date()
    if candidate < anchor.date() - timedelta(days=30):
        try:
            candidate = date(anchor.year + 1, month, day_of_month)
        except ValueError:
            pass
    return candidate


def _overlaps(span: tuple[int, int], claimed: list[tuple[int, int]]) -> bool:
    return any(not (span[1] <= start or span[0] >= end) for start, end in claimed)


def _trim_span(text: str, span: tuple[int, int]) -> tuple[int, int]:
    """去掉匹配两端的空白，保证证据高亮只覆盖真正的时间表达。"""
    start, end = span
    while end > start and text[end - 1].isspace():
        end -= 1
    while start < end and text[start].isspace():
        start += 1
    return (start, end)


def _resolve_time_match(match: re.Match[str]) -> time | None:
    try:
        hour = int(match.group("h"))
        minute = int(match.group("m")) if match.group("m") else 0
    except (TypeError, ValueError):
        return None
    if match.group("half"):
        minute = 30
    period = match.group("period") or ""
    if hour > 24 or minute > 59:
        return None
    if period in {"下午", "傍晚", "晚上", "夜里"}:
        if hour < 12:
            hour += 12
    elif period in {"中午", "正午"}:
        if hour <= 2:
            hour += 12
    elif period == "凌晨":
        if hour == 12:
            hour = 0
    if hour > 23:
        hour = hour % 24
    return time(hour, minute)


def _resolve_date_cand(kind: str, match: re.Match[str], text: str, anchor: datetime) -> _DateCand | None:
    span = match.span()
    raw = match.group(0)
    if kind == "ymd":
        try:
            day = date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        except ValueError:
            return None
        return _DateCand(span=span, kind=kind, text=raw, day=day)
    if kind == "md":
        try:
            day = _infer_year(int(match.group("m")), int(match.group("d")), anchor)
        except ValueError:
            return None
        return _DateCand(span=span, kind=kind, text=raw, day=day)
    if kind == "d":
        try:
            day = _infer_year(anchor.month, int(match.group("d")), anchor)
        except ValueError:
            return None
        return _DateCand(span=span, kind=kind, text=raw, day=day)
    if kind == "rel":
        token = match.group("rel")
        delta = RELATIVE_DAYS.get(token)
        if delta is None:
            return None
        # 「今晚8点」「明早9点」的时段信息藏在日期词里，需要传给时刻解析
        period_hint = None
        if token.endswith("晚"):
            period_hint = "night"
        elif token.endswith("早"):
            period_hint = "morning"
        return _DateCand(
            span=span, kind=kind, text=raw,
            day=anchor.date() + timedelta(days=delta), period_hint=period_hint,
        )
    if kind == "week":
        prefix = match.group("prefix") or ""
        weekday = WEEKDAY_MAP.get(match.group("wd"))
        if weekday is None:
            return None
        delta = weekday - anchor.weekday()
        if prefix in {"", "每"} and delta < 0:
            delta += 7
        if prefix in {"下周", "下个星期", "下个周"}:
            delta += 7
        elif prefix == "下下":
            delta += 14
        elif prefix == "上周":
            delta -= 7
        return _DateCand(span=span, kind=kind, text=raw, day=anchor.date() + timedelta(days=delta))
    if kind == "term":
        week_no = cn_to_int(match.group("n"))
        if not week_no or week_no < 1:
            return None
        start = term_start_date()
        day = start + timedelta(days=(week_no - 1) * 7)
        tail = text[match.end(): match.end() + 6]
        tail_match = re.match(r"\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", tail)
        if tail_match:
            offset = WEEKDAY_MAP.get(tail_match.group(1))
            if offset is not None:
                day = day + timedelta(days=offset)
        return _DateCand(span=span, kind=kind, text=raw, day=day)
    if kind == "dayoffset":
        offset = cn_to_int(match.group("n"))
        if offset is None:
            return None
        return _DateCand(span=span, kind=kind, text=raw, day=anchor.date() + timedelta(days=offset))
    if kind == "houroffset":
        offset = cn_to_int(match.group("n"))
        if offset is None:
            return None
        return _DateCand(span=span, kind=kind, text=raw, exact=anchor + timedelta(hours=offset))
    return None


def _find_ranges(text: str, claimed: list[tuple[int, int]]) -> list[tuple[tuple[int, int], time, time, str]]:
    found = []
    for match in _RANGE_RE.finditer(text):
        if _overlaps(match.span(), claimed):
            continue
        first = next(_TIME_RE.finditer(match.group("a").strip()), None)
        second = next(_TIME_RE.finditer(match.group("b").strip()), None)
        if first is None or second is None:
            continue
        start_time = _resolve_time_match(first)
        end_time = _resolve_time_match(second)
        if start_time is None or end_time is None:
            continue
        # 「下午2点-4点」：后一个时间继承前一个的时段
        if start_time.hour >= 12 and end_time.hour < 12:
            end_time = time(end_time.hour + 12, end_time.minute)
        if end_time <= start_time:
            continue
        span = _trim_span(text, match.span())
        claimed.append(span)
        found.append((span, start_time, end_time, text[span[0]: span[1]]))
    return found


def extract_temporal(text: str, anchor: datetime) -> list[TemporalHit]:
    """抽取文本中全部时间表达并归一为绝对时间（升序）。"""
    if not text:
        return []
    claimed: list[tuple[int, int]] = []

    date_cands: list[_DateCand] = []
    for kind, pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if _overlaps(match.span(), claimed):
                continue
            cand = _resolve_date_cand(kind, match, text, anchor)
            if cand is None:
                continue
            claimed.append(match.span())
            date_cands.append(cand)
    date_cands.sort(key=lambda item: item.span[0])

    ranges = _find_ranges(text, claimed)

    time_cands: list[dict] = []
    for match in _TIME_RE.finditer(text):
        trimmed = _trim_span(text, match.span())
        if _overlaps(trimmed, claimed):
            continue
        moment = _resolve_time_match(match)
        if moment is None:
            continue
        claimed.append(trimmed)
        time_cands.append(
            {
                "time": moment,
                "span": trimmed,
                "text": text[trimmed[0]: trimmed[1]],
                "period": match.group("period") or "",
            }
        )
    time_cands.sort(key=lambda item: item["span"][0])

    hits: list[TemporalHit] = []
    consumed_times: set[int] = set()
    consumed_dates: set[int] = set()

    # 1) 区间优先绑定最近的前置日期
    for span, start_time, end_time, raw in ranges:
        day = None
        for index, cand in enumerate(date_cands):
            if cand.day is None or cand.span[1] > span[0]:
                continue
            gap = span[0] - cand.span[1]
            if gap <= 20:
                day = cand.day
                consumed_dates.add(index)
        day = day or (date_cands[0].day if len(date_cands) == 1 and date_cands[0].day else None) or anchor.date()
        hits.append(
            TemporalHit(dt=_aware(day, start_time), span=span, text=raw.strip(),
                        has_date=True, has_time=True, precision="md", kind_hint="range_start")
        )
        hits.append(
            TemporalHit(dt=_aware(day, end_time), span=span, text=raw.strip(),
                        has_date=True, has_time=True, precision="md", kind_hint="range_end")
        )

    # 2) 日期 + 紧随其后的时刻
    for index, cand in enumerate(date_cands):
        if index in consumed_dates:
            continue
        if cand.exact is not None:
            hits.append(
                TemporalHit(dt=cand.exact, span=cand.span, text=cand.text, has_date=True,
                            has_time=True, precision=cand.kind)
            )
            continue
        if cand.day is None:
            continue
        attached = None
        for time_index, item in enumerate(time_cands):
            if time_index in consumed_times:
                continue
            if item["span"][0] < cand.span[1]:
                continue
            if item["span"][0] - cand.span[1] > 12:
                continue
            # 若两者之间还夹着另一个日期，这个时刻多半属于那个日期，不能跨过去认领
            if any(
                other is not cand and cand.span[1] < other.span[0] < item["span"][0]
                for other in date_cands
            ):
                continue
            attached = (time_index, item)
            break
        if attached is not None:
            time_index, item = attached
            consumed_times.add(time_index)
            moment = item["time"]
            # 「今晚8点」：时段来自日期词而不是时刻本身
            if not item["period"] and cand.period_hint == "night" and moment.hour < 12:
                moment = time(moment.hour + 12, moment.minute)
            hits.append(
                TemporalHit(dt=_aware(cand.day, moment), span=(cand.span[0], item["span"][1]),
                            text=text[cand.span[0]: item["span"][1]], has_date=True, has_time=True,
                            precision=cand.kind)
            )
        else:
            hits.append(
                TemporalHit(dt=_aware(cand.day, time(0, 0)), span=cand.span, text=cand.text,
                            has_date=True, has_time=False, precision=cand.kind)
            )

    # 3) 只有时刻没有日期：按锚点当天理解，已过则顺延一天
    for time_index, item in enumerate(time_cands):
        if time_index in consumed_times:
            continue
        moment = item["time"]
        candidate = _aware(anchor.date(), moment)
        if candidate < anchor:
            candidate = candidate + timedelta(days=1)
        hits.append(
            TemporalHit(dt=candidate, span=item["span"], text=item["text"], has_date=False,
                        has_time=True, precision="time_only")
        )

    hits.sort(key=lambda hit: hit.span[0])
    return hits


def apply_default_time(dt: datetime, field: str | None) -> datetime:
    """日期缺时刻时补默认时刻（截止类默认 23:59，其余保持 00:00 并交由 UI 提示）。"""
    if dt.hour or dt.minute:
        return dt
    moment = default_time_for(field)
    if moment is None:
        return dt
    return dt.replace(hour=moment.hour, minute=moment.minute)


def resolve_datetime(text: str, anchor: datetime, *, field: str | None = None) -> datetime | None:
    """把一段文本解析为绝对时间；用于大模型返回的原文时间片段。"""
    hits = extract_temporal(text or "", anchor)
    if not hits:
        return None
    hit = hits[0]
    if not hit.has_time:
        return apply_default_time(hit.dt, field)
    return hit.dt


def deadline_default_time(field: str = FIELD_DEADLINE) -> time | None:
    return default_time_for(field)
