"""识别流水线：把一整批消息变成「待确认事项」。

流程（对应讨论确定的 [2]→[7] 步）：
    批次抽取 (LLM #1) → 时间归一 → 候选召回 → 三区间归并 → 字段冲突消解 → 待确认
灰区不静默决定：低置信或疑似重复只标记，等用户在确认页拍板（NFR-05）。
"""
from __future__ import annotations

import time as time_module
from dataclasses import dataclass, field as dc_field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from events.models import Event, EventMessage
from events.services import apply_incoming_fields, create_event, refresh_denormalized
from extraction import merge as merge_mod
from extraction.fields import DATETIME_FIELDS, RELATION_PRIMARY
from extraction.markers import has_correction_marker, has_supplement_marker, pick_relation
from extraction.merge import EventSnapshot, decide, decide_reason, field_hint_for_snapshot
from extraction.normalize import apply_default_time, resolve_datetime
from extraction.providers import ExtractionError, get_provider
from extraction.providers.base import ExtractedEvent
from extraction.providers.rules import RuleBasedProvider
from extraction.resolve import IncomingField, IngestPayload

ACTION_CREATED = "created"
ACTION_MERGED = "merged"
ACTION_REVIEW = "review"


class ExtractionFailed(RuntimeError):
    """识别服务不可用且不允许回退时抛出，由视图降级为「手动新建事项」。"""


@dataclass
class ProcessedEvent:
    event: Event
    action: str
    score: float = 0.0
    reasons: list[str] = dc_field(default_factory=list)
    note: str = ""


@dataclass
class BatchResult:
    batch: object
    provider: str
    processed: list[ProcessedEvent] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)
    ignored: list[dict] = dc_field(default_factory=list)
    duration_ms: int = 0
    error: str = ""

    @property
    def drafts(self) -> list[Event]:
        """需要用户确认的事项（去重；自动合并的目标草稿只算一次）。"""
        drafts: list[Event] = []
        seen: set[int] = set()
        for item in self.processed:
            if item.action not in (ACTION_CREATED, ACTION_REVIEW):
                continue
            if item.event.status != Event.Status.DRAFT or item.event.pk in seen:
                continue
            seen.add(item.event.pk)
            drafts.append(item.event)
        return drafts

    @property
    def merged(self) -> list[ProcessedEvent]:
        return [item for item in self.processed if item.action == ACTION_MERGED]


def _source_key(message_links) -> str:
    for message, _relation in message_links:
        if message is None:
            continue
        parts = [part for part in ((message.source_label or "").strip(), (message.sender or "").strip()) if part]
        if parts:
            return "|".join(parts)
    return ""


def build_payload(
    extracted: ExtractedEvent, messages_by_seq: dict, fallback_message
) -> IngestPayload:
    """把抽取结果转成可落库载荷，并在这一步完成时间归一。"""
    # 只关联一条消息时（规则抽取器、以及模型没给 message_index 的情况），
    # 这条消息就是所有字段的来源，不能因为索引缺失而错认成批次里的第一条
    linked_messages = [messages_by_seq.get(item.index) for item in extracted.messages]
    linked_messages = [message for message in linked_messages if message is not None]
    sole_message = linked_messages[0] if len(linked_messages) == 1 else None

    incoming: list[IncomingField] = []
    for item in extracted.fields:
        message = messages_by_seq.get(item.message_index) or sole_message or fallback_message
        anchor = message.anchor_time
        value_dt = item.resolved_dt
        if item.field in DATETIME_FIELDS:
            if value_dt is None:
                # 相对时间按「这条字段所属消息」的发送时间换算
                value_dt = resolve_datetime(item.value, anchor, field=item.field)
            elif not item.has_time:
                value_dt = apply_default_time(value_dt, item.field)
        incoming.append(
            IncomingField(
                field=item.field,
                value_text=item.value,
                value_dt=value_dt,
                confidence=item.confidence,
                sent_at=anchor,
                evidence_span=f"{item.span[0]}:{item.span[1]}" if item.span else "",
                source_message=message,
                is_correction=has_correction_marker(message.raw_text),
            )
        )

    links: list[tuple[object, str]] = []
    for relation in extracted.messages:
        message = messages_by_seq.get(relation.index)
        if message is not None:
            links.append((message, relation.relation))
    if not links:
        links = [(fallback_message, RELATION_PRIMARY)]

    raw_text = "\n".join(message.raw_text for message, _relation in links if message is not None)
    return IngestPayload(
        title=extracted.title,
        category=extracted.category,
        confidence=extracted.confidence,
        fields=incoming,
        message_links=links,
        raw_text=raw_text,
    )


def _review_reasons(payload: IngestPayload, extracted: ExtractedEvent) -> list[str]:
    reasons: list[str] = []
    datetime_fields = [item for item in payload.fields if item.field in DATETIME_FIELDS]
    if not datetime_fields:
        reasons.append("未识别到任何时间，请补充截止或开始时间")
    if extracted.confidence < 0.6:
        reasons.append("识别置信度较低，请逐项核对")
    if any(item.span is None for item in extracted.fields):
        reasons.append("部分字段未能定位到原文证据")
    if not payload.title or len(payload.title) < 4:
        reasons.append("事件名过短，请确认")
    return reasons


def process_batch(batch, *, provider=None, apply_merge: bool = True) -> BatchResult:
    """执行一次完整识别；返回各事件的处理结果。"""
    from extraction.models import ExtractionRun

    started = time_module.monotonic()
    owner = batch.owner
    messages = list(batch.messages.order_by("seq"))
    result = BatchResult(batch=batch, provider="none")

    if not messages:
        result.warnings.append("该批次没有可识别的消息。")
        return result

    provider = provider or get_provider()
    warnings: list[str] = []
    try:
        outcome = provider.extract(messages, batch=batch)
    except ExtractionError as exc:
        if (getattr(settings, "EXTRACTION_PROVIDER", "auto") or "auto").lower() == "auto":
            warnings.append(f"大模型识别失败，已回退本地规则抽取器：{exc}")
            provider = RuleBasedProvider()
            try:
                outcome = provider.extract(messages, batch=batch)
            except ExtractionError as inner:  # pragma: no cover - 规则器不联网，几乎不会失败
                ExtractionRun.objects.create(
                    owner=owner, batch=batch, provider=provider.name,
                    status=ExtractionRun.Status.ERROR, message_count=len(messages),
                    error=str(inner)[:2000],
                )
                raise ExtractionFailed(str(inner)) from inner
        else:
            ExtractionRun.objects.create(
                owner=owner, batch=batch, provider=provider.name,
                status=ExtractionRun.Status.ERROR, message_count=len(messages), error=str(exc)[:2000],
            )
            raise ExtractionFailed(str(exc)) from exc

    warnings.extend(outcome.warnings)
    messages_by_seq = {message.seq: message for message in messages}
    fallback_message = messages[0]

    # 本批次内已处理的事项也参与归并 —— 一次粘贴的多条消息理应互相补充
    batch_snapshots: list[tuple[Event, EventSnapshot]] = []
    processed: list[ProcessedEvent] = []

    # 已经属于某个事项的原文（重复导入 / 重新识别）必然归回原事项，
    # 否则「重新识别」会凭空多出一条重复草稿。
    linked_events = {
        link.message_id: link.event
        for link in EventMessage.objects.filter(message__in=messages).select_related("event")
    }

    for extracted in outcome.events:
        payload = build_payload(extracted, messages_by_seq, fallback_message)
        hint = field_hint_for_snapshot(payload.fields)
        snapshot = EventSnapshot(
            title=payload.title,
            raw_text=payload.raw_text,
            source_key=_source_key(payload.message_links),
            **hint,
        )

        candidates: list[tuple[Event, float, list[str], bool]] = []
        for event, snap in batch_snapshots:
            score, reasons = merge_mod.score_pair(snap, snapshot)
            candidates.append((event, score, reasons, True))
        for event, score, reasons in merge_mod.recall_candidates(owner, snapshot):
            candidates.append((event, score, reasons, False))
        for message, _relation in payload.message_links:
            linked = linked_events.get(message.pk)
            if linked is None:
                continue
            hint_reason = "该原文已属于此事项，识别结果直接并入"
            for index, item in enumerate(candidates):
                if item[0].pk == linked.pk:
                    # 就地提升为确定命中：否则「重新识别」可能落进灰区另建一条草稿
                    candidates[index] = (linked, 1.0, [*item[2], hint_reason], item[3])
                    break
            else:
                candidates.append((linked, 1.0, [hint_reason], False))
        candidates.sort(key=lambda item: item[1], reverse=True)

        best = candidates[0] if candidates else None
        action = decide(best[1]) if best else merge_mod.DECISION_NEW

        # 「更正 / 补充」字样的启发式：这类消息几乎必然指向刚发布的那条通知，而不是一件新事。
        # 若时间窗口内只有一个候选事项，则认定它：
        #   更正类表述（更正/改为/改到/延期/取消）→ 自动合并（用户仍可撤销合并）；
        #   补充类表述（补充/另外）→ 不静默合并，改由用户在确认页拍板。
        forced_reason = ""
        if best is not None:
            marker_correction = has_correction_marker(payload.raw_text)
            marker_supplement = has_supplement_marker(payload.raw_text)
            if marker_correction or marker_supplement:
                anchor_time = (
                    payload.primary_message.anchor_time if payload.primary_message else timezone.now()
                )
                window = [
                    item for item in candidates
                    if item[0].last_updated_at >= anchor_time - timedelta(days=3)
                ]
                if len(window) == 1:
                    forced = window[0]
                    if marker_correction:
                        best = (
                            forced[0], forced[1],
                            [*forced[2], "含更正/变更表述，且近期只有一个候选事项"], forced[3],
                        )
                        action = merge_mod.DECISION_MERGE
                        forced_reason = "按更正类表述自动归并到近期唯一候选事项（可在详情页撤销合并）"
                    elif decide(forced[1]) != merge_mod.DECISION_MERGE:
                        best = (
                            forced[0], forced[1],
                            [*forced[2], "含补充表述，且近期只有一个候选事项"], forced[3],
                        )
                        action = merge_mod.DECISION_REVIEW
                        forced_reason = "疑似是近期事项的补充说明"

        if action == merge_mod.DECISION_MERGE and best and apply_merge:
            target, score, reasons, _is_batch = best
            target_snapshot = merge_mod.snapshot_from_event(target)
            relation = pick_relation(
                payload.raw_text, title_identical=(target_snapshot.title_norm == snapshot.title_norm)
            )
            apply_incoming_fields(target, payload, relation=relation, score=score)
            if target.status == Event.Status.CONFIRMED:
                # 更正/延期必须驱动提醒重排：撤销旧排期并按新时间重建（NFR-04）
                from reminders.services import sync_reminders

                sync_reminders(target)
            processed.append(
                ProcessedEvent(
                    event=target, action=ACTION_MERGED, score=score, reasons=reasons,
                    note=forced_reason or decide_reason(score, reasons),
                )
            )
            for index, (event, _snap) in enumerate(batch_snapshots):
                if event.pk == target.pk:
                    batch_snapshots[index] = (target, merge_mod.snapshot_from_event(target))
            continue

        review_reasons = _review_reasons(payload, extracted)
        if forced_reason:
            review_reasons.insert(0, forced_reason)
        elif action == merge_mod.DECISION_REVIEW and best:
            review_reasons.insert(0, decide_reason(best[1], best[2]))
        needs_review = bool(review_reasons)
        event = create_event(
            owner,
            payload,
            batch=batch,
            status=Event.Status.DRAFT,
            review_needed=needs_review,
            review_candidate=best[0] if action == merge_mod.DECISION_REVIEW and best else None,
            review_note="；".join(review_reasons)[:300],
        )
        refresh_denormalized(event)
        batch_snapshots.append((event, merge_mod.snapshot_from_event(event)))
        processed.append(
            ProcessedEvent(
                event=event,
                action=ACTION_REVIEW if action == merge_mod.DECISION_REVIEW else ACTION_CREATED,
                score=best[1] if best else 0.0,
                reasons=best[2] if best else [],
                note="；".join(review_reasons)[:300],
            )
        )

    duration = int((time_module.monotonic() - started) * 1000)
    status = ExtractionRun.Status.OK if processed else ExtractionRun.Status.EMPTY
    ExtractionRun.objects.create(
        owner=owner,
        batch=batch,
        provider=outcome.provider,
        status=status,
        message_count=len(messages),
        event_count=len(processed),
        duration_ms=outcome.duration_ms or duration,
        warnings=warnings[:10],
    )

    batch.stats = {
        "provider": outcome.provider,
        "messages": len(messages),
        "events": len(processed),
        "merged": len([item for item in processed if item.action == ACTION_MERGED]),
        "ignored": len(outcome.ignored),
        "duration_ms": outcome.duration_ms or duration,
    }
    batch.save(update_fields=["stats"])

    result.provider = outcome.provider
    result.processed = processed
    result.warnings = warnings
    result.ignored = outcome.ignored
    result.duration_ms = duration
    if not processed:
        result.warnings.append("未识别到可执行事项，可手动新建（原文已保留）。")
    return result


def detach_message(event: Event, message, *, provider=None) -> Event | None:
    """撤销合并：把一条原文从事件中摘出，重新独立识别为一个待确认事项。"""
    from events.services import split_message_from_event

    provider = provider or get_provider()
    try:
        outcome = provider.extract([message], batch=event.batch)
    except ExtractionError:
        outcome = RuleBasedProvider().extract([message], batch=event.batch)

    new_event = None
    if outcome.events:
        payload = build_payload(outcome.events[0], {message.seq: message}, message)
        new_event = create_event(
            event.owner,
            payload,
            batch=event.batch,
            status=Event.Status.DRAFT,
            review_needed=True,
            review_note="由「撤销合并」拆出，请确认是否独立为单独事项。",
        )
    split_message_from_event(event, message)
    return new_event
