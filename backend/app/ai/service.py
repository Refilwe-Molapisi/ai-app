"""AI service — the governed generation core, shared by the /ai/answer route,
the Phase 4 workflow engine and the Phase 5 specialist agents.

One path for every model call: org-scoped retrieval (TV-012, TV-019) →
system-prompt render → model call → §20 ledger row (including failures).
Route- and agent-specific concerns (auth, which instructions) stay with the
callers; this module owns the loop.

Error contract (mapped by callers):
- RuntimeError          model/embedder provider not configured → 503, no ledger row
                        (a configuration gap is not a model call, §20)
- QueueUnavailable      DB unreachable → global 503 handler
- ModelCallError        the call itself failed — a ledger row with
                        status='error' has already been written (§20)
"""

import time

from app.ai.prompts import (
    DEFAULT_PROMPT_NAME, InvalidTemplate, default_template, format_context, render,
)
from app.core.config import Settings, get_settings


def _model():
    """Model resolution point — tests substitute here (no key needed)."""
    from app.adapters.model_adapter import get_model_adapter
    return get_model_adapter(get_settings())


class ModelCallError(RuntimeError):
    """The model provider was reached (or attempted) and the call failed.
    A §20 ledger row with status='error' has already been written."""


async def _retrieve(*, question: str, org_id: str, settings: Settings,
                    top_k: int | None = None, engagement_id: str | None = None):
    """Org-scoped knowledge retrieval with embedder pinning (Phase 2 store
    enforces both). Raises RuntimeError when no embedder is configured."""
    from app.adapters.embedding_adapter import get_embedding_adapter
    from app.knowledge.store import KnowledgeStore, to_pgvector

    embedder = get_embedding_adapter(settings)
    embedded = await embedder.embed([question])
    knowledge = KnowledgeStore(settings)
    try:
        hits = knowledge.search(
            org_id=org_id, query_vector=to_pgvector(embedded.vectors[0]),
            provider=embedded.provider, model=embedded.model,
            dimensions=embedded.dimensions,
            top_k=top_k or settings.knowledge_top_k,
            engagement_id=engagement_id,
        )
        index_stats = knowledge.stats(org_id)
    finally:
        knowledge.close()
    return hits, index_stats, embedded


async def _generate(*, system: str, question: str, org_id: str,
                    purpose: str, principal_label: str, settings: Settings):
    """One ledger-governed model call (ok AND error rows written here, §20)."""
    started = time.monotonic()
    adapter = _model()  # RuntimeError if unconfigured → 503 upstream
    from app.ai.store import AIStore
    ai = AIStore(settings)
    try:
        try:
            result = await adapter.complete(
                [{"role": "user", "content": question}],
                system=system,
                temperature=settings.ai_answer_temperature,
                max_tokens=settings.ai_max_answer_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — §20: failed calls are ledger events too
            ai.record_usage(
                org_id=org_id, principal=principal_label, purpose=purpose,
                provider=adapter.provider_name, model="unknown",
                duration_ms=int((time.monotonic() - started) * 1000),
                estimated_cost_usd=0.0, status="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise ModelCallError(f"model call failed: {type(exc).__name__}: {exc}") from exc

        ai.record_usage(
            org_id=org_id, principal=principal_label, purpose=purpose,
            provider=result.provider, model=result.model,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            cached_tokens=result.cached_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            estimated_cost_usd=result.estimated_cost_usd,
            status="ok", detail=question[:200],
        )
    finally:
        ai.close()
    return result


def _payload(*, result, hits, index_stats, embedded, prompt_name, prompt_version) -> dict:
    """Shared response shape for answers and agent runs."""
    grounded = bool(hits)
    return {
        "answer": result.text,
        "grounded": grounded,
        "citations": [
            {"document_id": h["document_id"], "title": h["title"], "seq": h["seq"],
             "distance": h["distance"]}
            for h in hits
        ],
        "prompt": {"name": prompt_name, "version": prompt_version},
        "retrieval": {
            "embedder": {"provider": embedded.provider, "model": embedded.model,
                         "dimensions": embedded.dimensions},
            "chunks_in_index": index_stats["chunks"],
            "hint": None if grounded else (
                "no chunks indexed for this org yet" if not index_stats["chunks"]
                else "index was built with a different embedder — re-index documents (TV-019)"),
        },
        "usage": {
            "provider": result.provider, "model": result.model,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "estimated_cost_usd": result.estimated_cost_usd,
            "duration_seconds": result.duration_seconds,
        },
    }


async def grounded_answer(
    *,
    question: str,
    org_id: str,
    principal_label: str = "",
    top_k: int | None = None,
    engagement_id: str | None = None,
    settings=None,
) -> dict:
    """One governed answer: org-scoped retrieval, active prompt template
    (org version if it exists, default otherwise), model call, ledger row."""
    settings = settings or get_settings()

    hits, index_stats, embedded = await _retrieve(
        question=question, org_id=org_id, settings=settings,
        top_k=top_k, engagement_id=engagement_id,
    )

    from app.ai.store import AIStore
    ai = AIStore(settings)
    try:
        active = ai.get_active_prompt(org_id=org_id, name=DEFAULT_PROMPT_NAME)
        template = active or default_template().__dict__
        try:
            system = render(template["body"], format_context(hits))
        except InvalidTemplate as exc:  # stored template failing validation = operator error
            raise ValueError(f"active prompt template is invalid: {exc}") from exc
    finally:
        ai.close()

    result = await _generate(
        system=system, question=question, org_id=org_id,
        purpose="answer", principal_label=principal_label, settings=settings,
    )
    return _payload(
        result=result, hits=hits, index_stats=index_stats, embedded=embedded,
        prompt_name=template["name"], prompt_version=template.get("version"),
    )
