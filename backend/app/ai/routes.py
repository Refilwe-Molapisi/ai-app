"""AI integration API (Phase 3 — CAIOS-MASTER-001 §8, §9, §20).

    POST /ai/answer       grounded Q&A: retrieve → render template → model → ledger
    GET  /ai/usage        §20 ledger summary per org (tokens, cost, errors)
    POST /prompts         create a versioned prompt (v1 active)
    GET  /prompts         list prompts for an org
    GET  /prompts/{id}    prompt detail incl. all versions
    POST /prompts/{id}/versions  append + activate a new version (deliberate change)

The answer loop is a direct composition of the Phase 2 knowledge store and
the Phase 0–4 model adapter: retrieval is org-scoped (TV-012), templates are
versioned (§8.3), and EVERY model call writes a §20 ledger row — including
failures. Heavy/off-peak bulk generation stays on the job queue (Phase 6);
this endpoint answers a single question inside the HTTP budget.
"""

import logging
import time
import uuid
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.adapters.model_adapter import ModelResult
from app.api.deps import get_current_principal
from app.core.config import Settings, get_settings
from app.core.security import Principal
from app.ai.prompts import (
    DEFAULT_PROMPT_NAME, InvalidTemplate, default_template, format_context,
    render, validate_template,
)
from app.knowledge.store import KnowledgeStore

logger = logging.getLogger("caios.ai")
router = APIRouter(prefix="/ai", tags=["ai"])
prompt_router = APIRouter(prefix="/prompts", tags=["prompts"])


def _ai_store():
    from app.ai.store import AIStore
    return AIStore(get_settings())


def _model():
    """Model resolution point — tests substitute here (no key needed)."""
    from app.adapters.model_adapter import get_model_adapter
    return get_model_adapter(get_settings())


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    org_id: str = Field(min_length=1, max_length=120)
    top_k: int | None = Field(default=None, ge=1, le=50)
    engagement_id: str | None = None


class PromptCreate(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=16000)


class PromptVersionCreate(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=16000)


@router.post("/answer")
async def answer(
    body: AnswerRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    """Grounded answer: search the org's knowledge base, render the active
    prompt template, call the model, write the ledger row.

    Honesty rules baked in: `grounded=false` is reported when the knowledge
    base contributed nothing (empty index OR index built with a different
    embedder — never mix vector spaces), and the default template instructs
    the model to admit when the answer is not in the documents."""
    settings = get_settings()
    org_id = body.org_id.strip()
    if principal.client_org_id and principal.client_org_id != org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "queries are restricted to the caller's client organisation")

    # 1. Retrieve — org-scoped, embedder-pinned (Phase 2 store enforces both).
    from app.adapters.embedding_adapter import get_embedding_adapter
    from app.knowledge.store import to_pgvector
    try:
        embedder = get_embedding_adapter(settings)
        embedded = await embedder.embed([body.question])
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    knowledge = KnowledgeStore(settings)
    try:
        hits = knowledge.search(
            org_id=org_id, query_vector=to_pgvector(embedded.vectors[0]),
            provider=embedded.provider, model=embedded.model,
            dimensions=embedded.dimensions,
            top_k=body.top_k or settings.knowledge_top_k,
            engagement_id=body.engagement_id,
        )
        index_stats = knowledge.stats(org_id)
    finally:
        knowledge.close()
    grounded = bool(hits)

    # 2. Render the active template (org version if it exists, default otherwise).
    ai = _ai_store()
    try:
        active = ai.get_active_prompt(org_id=org_id, name=DEFAULT_PROMPT_NAME)
        template = active or default_template().__dict__
        try:
            system = render(template["body"], format_context(hits))
        except InvalidTemplate as exc:  # a stored template failing validation = operator error
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                f"active prompt template is invalid: {exc}") from exc

        # 3. Call the model — one question, one answer, ledger row either way.
        started = time.monotonic()
        try:
            result: ModelResult = await _model().complete(
                [{"role": "user", "content": body.question}],
                system=system,
                temperature=settings.ai_answer_temperature,
                max_tokens=settings.ai_max_answer_tokens,
            )
            ai.record_usage(
                org_id=org_id, principal=principal.subject, purpose="answer",
                provider=result.provider, model=result.model,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cached_tokens=result.cached_tokens,
                duration_ms=int((time.monotonic() - started) * 1000),
                estimated_cost_usd=result.estimated_cost_usd,
                status="ok", detail=body.question[:200],
            )
        except Exception as exc:  # noqa: BLE001 — §20: failed calls are ledger events too
            ai.record_usage(
                org_id=org_id, principal=principal.subject, purpose="answer",
                provider=getattr(_safe_adapter_probe(), "provider_name", "unknown"),
                model="unknown",
                duration_ms=int((time.monotonic() - started) * 1000),
                estimated_cost_usd=0.0, status="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                "model call failed; the error was recorded in the usage ledger") from exc
    finally:
        ai.close()

    return {
        "answer": result.text,
        "grounded": grounded,
        "citations": [
            {"document_id": h["document_id"], "title": h["title"], "seq": h["seq"],
             "distance": h["distance"]}
            for h in hits
        ],
        "prompt": {"name": template["name"], "version": template.get("version")},
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


class _AdapterProbe:
    provider_name = "unknown"


def _safe_adapter_probe() -> _AdapterProbe:
    try:
        from app.adapters.model_adapter import get_model_adapter
        return get_model_adapter(get_settings())
    except Exception:  # noqa: BLE001 — probing must never mask the original error
        return _AdapterProbe()


@router.get("/usage")
async def usage(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
    days: int = 30,
) -> dict:
    ai = _ai_store()
    try:
        return ai.usage_summary(org_id=org_id, days=days)
    finally:
        ai.close()


# ── Prompt management ────────────────────────────────────────────────────────

@prompt_router.post("", status_code=status.HTTP_201_CREATED)
async def create_prompt(
    body: PromptCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    try:
        validate_template(body.body)
    except InvalidTemplate as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    ai = _ai_store()
    try:
        try:
            prompt = ai.create_prompt(org_id=body.org_id.strip(), name=body.name,
                                      body=body.body, description=body.description,
                                      created_by=principal.subject)
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"a prompt named '{body.name}' already exists for this org") from exc
    finally:
        ai.close()
    return prompt


@prompt_router.get("")
async def list_prompts(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    ai = _ai_store()
    try:
        prompts = ai.list_prompts(org_id=org_id)
    finally:
        ai.close()
    return {"org_id": org_id, "count": len(prompts), "prompts": prompts}


@prompt_router.get("/{prompt_id}")
async def prompt_detail(
    prompt_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    ai = _ai_store()
    try:
        prompt = ai.get_prompt(prompt_id, org_id=org_id)
    finally:
        ai.close()
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found in this organisation")
    return prompt


@prompt_router.post("/{prompt_id}/versions", status_code=status.HTTP_201_CREATED)
async def add_prompt_version(
    prompt_id: uuid.UUID,
    body: PromptVersionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    try:
        validate_template(body.body)
    except InvalidTemplate as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    ai = _ai_store()
    try:
        version = ai.add_version(prompt_id, org_id=body.org_id.strip(),
                                 body=body.body, created_by=principal.subject)
    finally:
        ai.close()
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found in this organisation")
    return version
