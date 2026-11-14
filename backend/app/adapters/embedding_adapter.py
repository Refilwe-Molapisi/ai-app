"""Embedding adapter — independent of the primary LLM provider (ADR-012, §9.4).

Embeddings are how CAIOS retrieves approved company knowledge (RAG, §9). The
choice of embedder must never be coupled to the choice of chat model: this
adapter interface ships three implementations —

1. DeterministicHashEmbedder — zero-dependency, deterministic; development and
   tests only. NOT semantically meaningful; exists so the whole RAG pipeline
   can run end-to-end without any external service.
2. OpenAICompatEmbedder — any OpenAI-compatible /v1/embeddings endpoint
   (Ollama, TEI, a future API provider).
3. (Target) local BGE-M3 / multilingual-e5 in the worker container — added in
   Phase 2 behind this same interface; nothing else changes.

Every embedding batch records provider/model/dimensions alongside the vectors
so a model change triggers controlled re-indexing (TV-019).
"""

import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.core.config import Settings, get_settings


@dataclass
class EmbeddingBatch:
    vectors: list[list[float]]
    provider: str
    model: str
    dimensions: int


class EmbeddingAdapter(ABC):
    provider_name: str = "abstract"

    @abstractmethod
    async def embed(self, texts: list[str]) -> EmbeddingBatch: ...


class DeterministicHashEmbedder(EmbeddingAdapter):
    """Stable pseudo-embeddings from a text hash. Dev/tests only."""

    provider_name = "hash-dev"
    DIMENSIONS = 256

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        vectors = [self._one(t) for t in texts]
        return EmbeddingBatch(
            vectors=vectors, provider=self.provider_name,
            model="caios-hash-256", dimensions=self.DIMENSIONS,
        )

    def _one(self, text: str) -> list[float]:
        out: list[float] = []
        seed = text.strip().lower()
        while len(out) < self.DIMENSIONS:
            digest = hashlib.sha256(f"{seed}:{len(out)}".encode()).digest()
            out.extend(int.from_bytes(digest[i:i + 4], "big") for i in range(0, 32, 4))
        vec = out[: self.DIMENSIONS]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAICompatEmbedder(EmbeddingAdapter):
    """Calls any OpenAI-compatible embeddings endpoint (Ollama/TEI/provider)."""

    provider_name = "openai-compat"

    def __init__(self, settings: Settings):
        if not settings.embedding_endpoint:
            raise RuntimeError("EMBEDDING_ENDPOINT is not configured")
        self._endpoint = settings.embedding_endpoint
        self._model = settings.embedding_model or "bge-m3"

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                self._endpoint,
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        vectors = [item["embedding"] for item in data]
        return EmbeddingBatch(
            vectors=vectors, provider=self.provider_name, model=self._model,
            dimensions=len(vectors[0]) if vectors else 0,
        )


def get_embedding_adapter(settings: Settings | None = None) -> EmbeddingAdapter:
    settings = settings or get_settings()
    if settings.embedding_endpoint:
        return OpenAICompatEmbedder(settings)
    return DeterministicHashEmbedder()  # dev fallback — flagged in usage records
