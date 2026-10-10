"""OpenAI 兼容接口的大模型抽取 Provider。

只依赖标准库 urllib，避免为一个 HTTP 调用引入额外依赖；base_url/模型可配置，
便于替换 DeepSeek / 通义 / 自建 vLLM 等服务（需求 5.5-3）。
"""
from __future__ import annotations

import json
import re
import time as time_module
import urllib.error
import urllib.request

from django.conf import settings
from django.utils import timezone

from extraction.fields import (
    CATEGORY_CHOICES,
    CATEGORY_OTHER,
    FIELD_CHOICES,
    RELATION_CHOICES,
    RELATION_PRIMARY,
)
from extraction.providers.base import (
    ExtractedEvent,
    ExtractionOutcome,
    ExtractionProvider,
    FieldValue,
    MessageRelation,
    VALID_FIELDS,
)

SYSTEM_PROMPT = """你是校园通知结构化抽取引擎。输入是一批带编号的群消息或通知原文。

工作要求：
1. 先判断每条消息是否包含可执行的校园事项，纯闲聊归入 ignored。
2. 多条消息可能描述同一件事：补充信息用 relation=supplement，明确更正时间/地点用 correction，
   跨渠道重复发布用 duplicate，首次出现用 primary。
3. 一条消息里若包含多个互不相关的事项，请拆分成多个 events。
4. 时间字段（deadline/start_at/end_at）的 value 必须原样照抄原文中的时间表达
   （如「下周三下午3点」「10月20日」），不要自行换算成绝对日期，系统会统一换算。
5. 每个字段都要给出 evidence：原文中最短的、能原样匹配到的证据片段。
6. 不确定或原文没有的信息直接省略，禁止编造。
7. 相对时间以每条消息头部的时间戳为基准理解。

只输出一个 JSON 对象，不要输出解释或 Markdown 代码块：
{
  "events": [
    {
      "title": "简洁的事件名（不超过30字）",
      "category": "course|exam|homework|activity|contest|fee|other",
      "confidence": 0.0-1.0,
      "messages": [{"index": 1, "relation": "primary|supplement|correction|duplicate"}],
      "fields": [
        {
          "field": "deadline|start_at|end_at|location|course|organizer|link|contact|requirement",
          "value": "字段值（时间字段照抄原文表达）",
          "evidence": "原文证据片段",
          "confidence": 0.0-1.0,
          "message_index": 1
        }
      ]
    }
  ],
  "ignored": [{"index": 2, "reason": "闲聊"}]
}"""

RECHECK_PROMPT = """你要判断两个校园事项记录是否描述同一件事。
只输出 JSON：{"same": true 或 false, "reason": "一句话理由"}
判定要点：同一活动/作业/考试的重复发布、补充或更正算同一件事；
同一门课的两次不同作业（如作业1与作业2）、周期性的例会不算同一件事。"""


class ExtractionError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S)
    if fence:
        return fence.group(1).strip()
    return text


def _load_json(text: str) -> dict:
    cleaned = _strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 容错：截取第一个 { 到最后一个 }
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start: end + 1])
            except json.JSONDecodeError as exc:
                raise ExtractionError(f"模型返回不是合法 JSON：{exc}") from exc
        raise ExtractionError("模型返回不是合法 JSON")


def _clamp(value, default: float = 0.6) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, round(number, 3)))


def find_span(text: str, evidence: str) -> tuple[int, int] | None:
    """把模型给出的证据片段定位回原文，用于前端高亮（防幻觉的关键一环）。"""
    if not evidence:
        return None
    index = text.find(evidence)
    if index >= 0:
        return (index, index + len(evidence))
    stripped = evidence.strip()
    if stripped and stripped != evidence:
        index = text.find(stripped)
        if index >= 0:
            return (index, index + len(stripped))
    return None


class OpenAICompatProvider(ExtractionProvider):
    name = "llm"
    is_llm = True
    supports_recheck = True

    def __init__(self, *, base_url=None, api_key=None, model=None, timeout=None):
        self.base_url = (base_url or getattr(settings, "LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or getattr(settings, "LLM_API_KEY", "")
        self.model = model or getattr(settings, "LLM_MODEL", "deepseek-chat")
        self.timeout = int(timeout or getattr(settings, "LLM_TIMEOUT", 20))

    # ------------------------------------------------------------------
    def _chat(self, system: str, user: str, *, json_mode: bool = True) -> str:
        if not self.api_key:
            raise ExtractionError("未配置 LLM_API_KEY")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(getattr(settings, "LLM_TEMPERATURE", 0.1)),
            "max_tokens": int(getattr(settings, "LLM_MAX_TOKENS", 2000)),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - 依赖外部服务
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            raise ExtractionError(f"识别服务返回 {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover
            raise ExtractionError(f"识别服务不可用：{exc}") from exc
        try:
            data = json.loads(body)
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise ExtractionError("识别服务返回结构异常") from exc

    # ------------------------------------------------------------------
    def extract(self, messages, *, batch=None) -> ExtractionOutcome:
        started = time_module.monotonic()
        if not messages:
            return ExtractionOutcome(events=[], provider=self.name, duration_ms=0)

        prompt = self._build_prompt(messages)
        content = self._chat(SYSTEM_PROMPT, prompt)
        data = _load_json(content)

        by_index = {message.seq: message for message in messages}
        events: list[ExtractedEvent] = []
        warnings: list[str] = []

        for raw_event in data.get("events") or []:
            if not isinstance(raw_event, dict):
                continue
            title = str(raw_event.get("title") or "").strip()[:80]
            if not title:
                continue
            category = str(raw_event.get("category") or CATEGORY_OTHER).strip()
            if category not in {value for value, _ in CATEGORY_CHOICES}:
                category = CATEGORY_OTHER

            fields: list[FieldValue] = []
            for raw_field in raw_event.get("fields") or []:
                if not isinstance(raw_field, dict):
                    continue
                name = str(raw_field.get("field") or "").strip()
                if name not in VALID_FIELDS:
                    continue
                value = str(raw_field.get("value") or "").strip()
                if not value:
                    continue
                try:
                    message_index = int(raw_field.get("message_index") or 1)
                except (TypeError, ValueError):
                    message_index = 1
                source = by_index.get(message_index)
                evidence = str(raw_field.get("evidence") or value)
                span = find_span(source.raw_text, evidence) if source else None
                if source is not None and span is None:
                    warnings.append(f"字段「{name}」的证据未能定位到原文，已标记为待确认。")
                fields.append(
                    FieldValue(
                        field=name,
                        value=value[:300],
                        confidence=_clamp(raw_field.get("confidence")),
                        span=span,
                        message_index=message_index,
                    )
                )
            if not fields:
                continue

            relations: list[MessageRelation] = []
            if isinstance(raw_event.get("messages"), list):
                for item in raw_event["messages"]:
                    if not isinstance(item, dict):
                        continue
                    try:
                        index = int(item.get("index"))
                    except (TypeError, ValueError):
                        continue
                    relation = str(item.get("relation") or RELATION_PRIMARY)
                    if relation not in {value for value, _ in RELATION_CHOICES}:
                        relation = RELATION_PRIMARY
                    relations.append(MessageRelation(index=index, relation=relation))
            if not relations:
                relations = [MessageRelation(index=message.seq) for message in messages]

            events.append(
                ExtractedEvent(
                    title=title,
                    category=category,
                    confidence=_clamp(raw_event.get("confidence"), 0.75),
                    fields=fields,
                    messages=relations,
                    raw=raw_event,
                )
            )

        duration = int((time_module.monotonic() - started) * 1000)
        return ExtractionOutcome(
            events=events,
            provider=self.name,
            ignored=[item for item in (data.get("ignored") or []) if isinstance(item, dict)],
            warnings=warnings,
            duration_ms=duration,
        )

    def recheck_merge(self, existing: dict, draft: dict) -> tuple[bool | None, str]:
        user = json.dumps({"现有事项": existing, "新识别事项": draft}, ensure_ascii=False, indent=2)
        try:
            content = self._chat(RECHECK_PROMPT, user)
            data = _load_json(content)
        except ExtractionError as exc:
            return None, str(exc)
        same = data.get("same")
        reason = str(data.get("reason") or "")
        if isinstance(same, bool):
            return same, reason
        return None, reason

    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(messages) -> str:
        blocks = []
        for message in messages:
            local = timezone.localtime(message.anchor_time).strftime("%Y-%m-%d %H:%M")
            source = message.source_label or "未标注来源"
            sender = message.sender or "未知发送者"
            blocks.append(f"[#{message.seq} | {source} | {sender} | {local}]\n{message.raw_text}")
        return "\n\n".join(blocks)

    def describe(self) -> str:
        return f"大模型抽取（{self.model}）"
