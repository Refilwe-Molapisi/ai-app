"""AI integration API (Phase 3 — CAIOS-MASTER-001 §8, §9, §20).

    POST /ai/answer       grounded Q&A: retrieve → render template → model → ledger
    GET  /ai/usage        §20 ledger summary per org (tokens, cost, errors)
    POST /prompts         create a versioned prompt (v1 active)
    GET  /prompts         list prompts for an org
    GET  /prompts/{id}    prompt detail incl. all versions
    POST /prompts/{id}/versions  append + activate a new version (deliberate change)

The answer loop itself lives in app/ai/service.py so the Phase 4 workflow
engine executes the exact same governed path; this module adds auth, the
client-org constraint and HTTP error mapping. Heavy/off-peak bulk generation
stays on the job queue (Phase 6); /ai/answer answers a single question inside
the HTTP budget.
"""

import logging
import uuid
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.ai.prompts import InvalidTemplate, validate_template
from app.ai.service import ModelCallError, grounded_answer
from app.api.deps import get_current_principal
from app.core.config import get_settings
from app.core.security import Principal

logger = logging.getLogger("caios.ai")
router = APIRouter(prefix="/ai", tags=["ai"])
prompt_router = APIRouter(prefix="/prompts", tags=["prompts"])


def _ai_store():
    from app.ai.store import AIStore
    return AIStore(get_settings())


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
    org_id = body.org_id.strip()
    if principal.client_org_id and principal.client_org_id != org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "queries are restricted to the caller's client organisation")
    try:
        return await grounded_answer(
            question=body.question, org_id=org_id,
            principal_label=principal.subject,
            top_k=body.top_k, engagement_id=body.engagement_id,
        )
    except ModelCallError as exc:
        # Reaching the provider failed — ledger row already written (§20).
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "model call failed; the error was recorded in the usage ledger") from exc
    except RuntimeError as exc:
        # Model/embedder provider not configured — clean 503, no ledger row
        # (a configuration gap is not a model call).
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:  # invalid stored template = operator error, loudly
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


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
