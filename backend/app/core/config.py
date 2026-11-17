"""CAIOS application configuration — the only place environment variables are read.

Contract: CAIOS-MASTER-001 §19 (credentials server-side only, separated per
environment). See infra/.env.example for the full variable list.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # Runtime
    caios_env: str = "development"  # development | staging | production

    # Database
    database_url: str = "postgresql://caios:caios_local_dev@db:5432/caios"

    # Auth (Supabase)
    supabase_url: str = ""
    supabase_jwks_url: str = ""
    supabase_audience: str = "authenticated"

    # Model adapter (DeepSeek initial provider — replaceable, ADR-006)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Embedding adapter (independent of the LLM — ADR-012)
    embedding_endpoint: str = ""
    embedding_model: str = ""

    # Storage adapter (Z1 S3-compatible — ADR-003)
    storage_endpoint: str = ""
    storage_bucket: str = ""
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_region: str = "za-region"

    # Job runner
    job_run_secret: str = "change-me-local"
    job_batch_size: int = 5
    job_time_budget_seconds: int = 25
    job_max_attempts: int = 3

    # Knowledge & documents (Phase 2 — chunking/retrieval defaults are safe)
    knowledge_chunk_chars: int = 1200      # ≈ 300–400 tokens per chunk
    knowledge_chunk_overlap: int = 150     # word-aligned overlap between chunks
    knowledge_embed_batch: int = 32        # texts per embedder call
    knowledge_top_k: int = 6               # default search depth
    knowledge_max_upload_mb: int = 20      # per-file upload cap

    # AI answering (Phase 3 — §8, §20)
    ai_answer_temperature: float = 0.2
    ai_max_answer_tokens: int = 1024

    # Email (Resend — Phase 4+)
    resend_api_key: str = ""
    email_from: str = "CAIOS <noreply@example.com>"

    @property
    def is_production(self) -> bool:
        return self.caios_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
