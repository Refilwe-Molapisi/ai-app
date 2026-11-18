"""Specialist agent execution — one governed path per run (§11, §20).

run_agent() composes the shared AI primitives:
    org-scoped retrieval (TV-012/TV-019) → agent instructions (org versioned
    override or §11 defaults) rendered with the retrieved context → model call
    → §20 ledger row (purpose='agent', including failures).

The agent's §11 constraints ride in the system prompt on every call, so an
org override can reword duties but the defaults' honesty rules and the
grounding block are enforced structurally: an override without {{context}}
is refused, never silently ungrounded.
"""

import logging

from app.agents.definitions import (
    CONTEXT_PLACEHOLDER, PROMPT_PREFIX, UnknownAgent, get_agent,
)
from app.ai.prompts import InvalidTemplate, format_context, render
from app.ai.service import _generate, _payload, _retrieve
from app.core.config import Settings, get_settings

logger = logging.getLogger("caios.agents")


async def run_agent(
    *,
    agent_slug: str,
    question: str,
    org_id: str,
    principal_label: str = "",
    top_k: int | None = None,
    engagement_id: str | None = None,
    settings: Settings | None = None,
) -> dict:
    """One governed agent run. Returns the shared payload shape plus the
    agent identity block; raises UnknownAgent for bad slugs, RuntimeError for
    unconfigured providers, ModelCallError after writing the failure row."""
    settings = settings or get_settings()
    try:
        agent = get_agent(agent_slug)
    except UnknownAgent as exc:
        raise ValueError(str(exc)) from exc

    hits, index_stats, embedded = await _retrieve(
        question=question, org_id=org_id, settings=settings,
        top_k=top_k, engagement_id=engagement_id,
    )

    # Instructions: org's active versioned override ('agent.<slug>') or the
    # §11 defaults. An override that drops the grounding block is an operator
    # error — refuse loudly rather than run ungrounded.
    from app.ai.store import AIStore
    ai = AIStore(settings)
    try:
        override = ai.get_active_prompt(org_id=org_id, name=agent.prompt_name)
        body = override["body"] if override else agent.default_instructions
        prompt_version = override["version"] if override else None
    finally:
        ai.close()
    if CONTEXT_PLACEHOLDER not in body:
        raise ValueError(
            f"prompt '{agent.prompt_name}' lacks {CONTEXT_PLACEHOLDER} — "
            "agent instructions must stay grounded; fix the template version")
    try:
        system = render(body, format_context(hits))
    except InvalidTemplate as exc:
        raise ValueError(f"agent instructions are invalid: {exc}") from exc

    result = await _generate(
        system=system, question=question, org_id=org_id,
        purpose="agent", principal_label=principal_label, settings=settings,
    )
    payload = _payload(
        result=result, hits=hits, index_stats=index_stats, embedded=embedded,
        prompt_name=agent.prompt_name, prompt_version=prompt_version,
    )
    payload["agent"] = {"slug": agent.slug, "name": agent.name,
                        "division": agent.division}
    return payload
