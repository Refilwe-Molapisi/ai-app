"""Model adapter — provider-independent LLM interface (ADR-006, §8.2).

The application talks only to `ModelAdapter`. Swapping DeepSeek for another
provider (or a self-hosted open-weight model) means adding a subclass —
specialist agents and workflows are untouched.

Usage records feed the AI Usage Management module (§20): provider, model, token
counts, cost estimate, duration, status. Cost constants are indicative and must
be verified against provider pricing before production use.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings


@dataclass
class ModelResult:
    text: str
    tool_calls: list = field(default_factory=list)
    finish_reason: str | None = None
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    duration_seconds: float = 0.0
    estimated_cost_usd: float = 0.0


class ModelAdapter(ABC):
    provider_name: str = "abstract"

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        json_schema: dict | None = None,   # structured outputs (§8.2)
        tools: list[dict] | None = None,   # tool calling (§8.2)
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ModelResult: ...

    @abstractmethod
    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float: ...


class DeepSeekAdapter(ModelAdapter):
    """DeepSeek via its OpenAI-compatible API (ADR-006).

    Pricing constants below are INDICATIVE (peak/off-peak + cache structure,
    Nov 2026) — TV-007 requires verification against current provider pricing
    before production use. Off-peak bulk runs are scheduled via the job queue
    (docs/01-hosting-blueprint.md §5).
    """

    provider_name = "deepseek"

    # USD per 1M tokens — verify in TV-007.
    PRICE_INPUT_PEAK = 0.27
    PRICE_INPUT_OFFPEAK = 0.135
    PRICE_OUTPUT_PEAK = 1.10
    PRICE_OUTPUT_OFFPEAK = 0.55

    def __init__(self, settings: Settings):
        from openai import AsyncOpenAI  # imported lazily: optional at import time

        if not settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        self._client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
        self._model = settings.deepseek_model

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        json_schema: dict | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ModelResult:
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        kwargs: dict = {
            "model": self._model,
            "messages": full_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "caios_structured_output", "strict": True,
                                "schema": json_schema},
            }
        if tools is not None:
            kwargs["tools"] = tools

        started = time.monotonic()
        resp = await self._client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0

        choice = resp.choices[0] if resp.choices else None
        tool_calls = []
        if choice is not None and getattr(choice.message, "tool_calls", None):
            tool_calls = [
                {"id": tc.id, "name": tc.function.name,
                 "arguments": tc.function.arguments}
                for tc in choice.message.tool_calls
            ]

        return ModelResult(
            text=(choice.message.content or "") if choice is not None else "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason if choice is not None else None,
            provider=self.provider_name,
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            duration_seconds=round(time.monotonic() - started, 3),
            estimated_cost_usd=self.estimate_cost_usd(input_tokens, output_tokens),
        )

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        # Conservative: priced at peak until the scheduler enforces off-peak runs.
        cost = (input_tokens / 1e6) * self.PRICE_INPUT_PEAK
        cost += (output_tokens / 1e6) * self.PRICE_OUTPUT_PEAK
        return round(cost, 6)


def get_model_adapter(settings: Settings | None = None) -> ModelAdapter:
    """Provider selection point — add adapters here, nowhere else."""
    settings = settings or get_settings()
    if settings.deepseek_api_key:
        return DeepSeekAdapter(settings)
    raise RuntimeError(
        "No model provider configured (DEEPSEEK_API_KEY missing). "
        "Assignment execution requiring the LLM cannot proceed."
    )
