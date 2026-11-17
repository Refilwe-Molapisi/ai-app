"""Prompt & instruction templates — versioned, validated, grounded (§8.3, §20).

CAIOS's behaviour is controlled by VERSIONED templates, never hardcoded prompt
strings scattered through handlers: what the model is told must be reviewable,
revertible and attributable. Rules enforced here:

- exactly one legal placeholder, {{context}} — where retrieved chunks go;
- the question never lands in the system prompt (it travels as the user
  message, keeping template reuse clean);
- the default template demands grounded answers with citations and explicit
  "not in the knowledge base" admissions — hallucinated authority is the
  failure mode this system must never exhibit (§9).
"""

import re
from dataclasses import dataclass

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
ALLOWED_PLACEHOLDERS = ("context",)


@dataclass
class PromptTemplate:
    name: str
    version: int | None
    body: str


class InvalidTemplate(ValueError):
    """Template rejected at creation or update time — never at answer time."""


def validate_template(body: str) -> None:
    if not body or not body.strip():
        raise InvalidTemplate("template body must not be empty")
    found = [m.group(1) for m in _PLACEHOLDER.finditer(body)]
    unknown = [p for p in found if p not in ALLOWED_PLACEHOLDERS]
    if unknown:
        raise InvalidTemplate(
            f"unknown placeholder(s) {unknown}; allowed: {list(ALLOWED_PLACEHOLDERS)}")
    if "context" not in found:
        raise InvalidTemplate("template must contain the {{context}} placeholder")


def render(body: str, context: str) -> str:
    """Fill the template. Raises if a placeholder survives rendering (template
    drift guard — validated templates make this unreachable, which is the point)."""
    out = _PLACEHOLDER.sub(lambda m: context if m.group(1) == "context" else m.group(0), body)
    if _PLACEHOLDER.search(out):
        raise InvalidTemplate("template still contains an unresolved placeholder after render")
    return out


def format_context(hits: list[dict]) -> str:
    """Render retrieval hits as the context block. Numbered so the model can
    cite [n]; distance shown so downstream humans can judge confidence."""
    if not hits:
        return ""
    parts = []
    for i, hit in enumerate(hits, start=1):
        parts.append(
            f"[{i}] {hit['title']} (chunk {hit['seq']}, distance {hit['distance']})\n"
            f"{hit['content']}"
        )
    return "\n\n".join(parts)


DEFAULT_PROMPT_NAME = "rag.answer"

DEFAULT_SYSTEM_PROMPT = """You are CAIOS, the AI operations assistant for this company. Answer the user's question using ONLY the context documents below when they are provided.

Rules:
- Cite the sources you used as [1], [2] … matching the context numbering.
- If the context does not contain the answer, say so plainly and answer only with well-established general knowledge, clearly marked as such.
- Never invent figures, dates, client names or obligations. South African tax and compliance context may apply.
- Be concise and operational: lead with the answer, then the supporting detail.

CONTEXT DOCUMENTS:
{{context}}"""


def default_template() -> PromptTemplate:
    return PromptTemplate(name=DEFAULT_PROMPT_NAME, version=None, body=DEFAULT_SYSTEM_PROMPT)
