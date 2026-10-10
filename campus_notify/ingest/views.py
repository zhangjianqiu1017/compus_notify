from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from ingest.models import Batch, Message
from ingest.services import DuplicateImport, create_batch, discard_raw_if_needed
from extraction.pipeline import ExtractionFailed, process_batch

SHARE_SESSION_KEY = "pending_share"


@login_required
@require_http_methods(["GET", "POST"])
def import_view(request: HttpRequest) -> HttpResponse:
    """FR-02：粘贴文字导入（MVP 主入口）。"""
    prefill = ""
    source = Batch.Source.PASTE
    if request.method == "GET":
        pending = request.session.pop(SHARE_SESSION_KEY, None)
        if pending:
            prefill = pending.get("text", "")
            source = Batch.Source.SHARE
            if pending.get("title"):
                messages.info(request, f"已接收分享内容：{pending['title']}")

    if request.method == "POST":
        raw = (request.POST.get("text") or "").strip()
        raw_source = request.POST.get("source") or Batch.Source.PASTE
        label = (request.POST.get("source_label") or "").strip()
        if raw_source not in dict(Batch.Source.choices):
            raw_source = Batch.Source.PASTE
        if not raw:
            messages.error(request, "请先粘贴或输入通知内容。")
            return render(
                request,
                "ingest/import.html",
                {"prefill": raw, "source": raw_source, "source_label": label},
            )

        try:
            batch, messages_created, warnings = create_batch(
                request.user,
                raw,
                source=raw_source,
                source_label=label,
                reuse_existing=(request.POST.get("reuse") == "1"),
            )
        except DuplicateImport as exc:
            # 不能只甩一句「完全重复」：要告诉用户撞上了哪一条、它现在关联了什么事项，
            # 并给出「仍然重新识别」的出口（此前识别结果被删掉时尤其需要）。
            return render(
                request,
                "ingest/import.html",
                {
                    "prefill": raw,
                    "source": raw_source,
                    "source_label": label,
                    "duplicates": exc.duplicates,
                    "duplicate_hint": str(exc),
                },
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "ingest/import.html",
                {"prefill": raw, "source": raw_source, "source_label": label},
            )

        for warning in warnings:
            messages.warning(request, warning)

        sensitive = [message for message in messages_created if message.has_sensitive]
        if sensitive:
            kinds = sorted({kind for message in sensitive for kind in message.sensitive_kinds.split(",") if kind})
            messages.warning(
                request,
                "原文中检测到可能敏感的信息（" + "、".join(kinds) + "），上传前请确认是否必要。",
            )

        try:
            result = process_batch(batch)
        except ExtractionFailed as exc:
            messages.error(request, f"识别服务暂不可用（{exc}）。原文已保留，可手动新建事项。")
            return redirect("events:create")

        # NFR-15：识别完成后按隐私设置决定是否保留原文
        discarded = discard_raw_if_needed(request.user, messages_created)
        if discarded:
            messages.info(request, f"按隐私设置已丢弃 {discarded} 条原文，仅保留结构化事项。")

        for warning in result.warnings:
            messages.warning(request, warning)

        drafts = result.drafts
        if len(drafts) == 1:
            return redirect("events:review", pk=drafts[0].pk)
        if len(drafts) > 1:
            messages.info(request, f"本次识别到 {len(drafts)} 个事项，请逐个确认。")
            return redirect(f"{_reverse_review_list()}?batch={batch.pk}")
        if result.merged:
            messages.success(
                request,
                f"内容已自动归并到已有事项：{'、'.join('《' + item.event.title + '》' for item in result.merged)}。",
            )
            return redirect("events:list")
        return redirect("events:list")

    return render(
        request,
        "ingest/import.html",
        {"prefill": prefill, "source": source},
    )


def _reverse_review_list() -> str:
    from django.urls import reverse

    return reverse("events:review_list")


@csrf_exempt
@require_POST
def share_target(request: HttpRequest) -> HttpResponse:
    """FR-02：系统分享导入（PWA share_target）。

    系统分享面板发起的是不带 CSRF token 的跨上下文 POST，因此这里豁免 CSRF。
    风险被限制在最小范围：本视图只把文本暂存到会话，不产生任何写库副作用，
    真正的识别必须由用户在导入页点击触发。
    """
    if not request.user.is_authenticated:
        request.session[SHARE_SESSION_KEY] = {
            "text": (request.POST.get("text") or request.POST.get("url") or "")[:20000],
            "title": request.POST.get("title", ""),
            "deferred": True,
        }
        return redirect("accounts:login")
    request.session[SHARE_SESSION_KEY] = {
        "text": (request.POST.get("text") or request.POST.get("url") or "")[:20000],
        "title": (request.POST.get("title") or "")[:200],
    }
    return redirect("ingest:import")


@login_required
def batch_detail(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(Batch, pk=pk, owner=request.user)
    return render(
        request,
        "ingest/batch.html",
        {
            "batch": batch,
            "messages": batch.messages.all(),
            "events": batch.events.all(),
            "runs": batch.runs.all()[:5],
        },
    )
