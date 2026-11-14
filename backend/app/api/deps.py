"""Shared API dependencies.

Two guardrails only:
- get_current_principal — Supabase-verified identity (app-level authorisation
  per §16.2; role/client scoping expands here in Phase 1).
- require_job_secret — shared-secret guard for cron/Actions-triggered
  endpoints (/jobs/run, /maintenance/tick). These endpoints are machine-facing
  and must never be exposed without the secret.
"""

import hmac

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.security import Principal, get_token, verify_supabase_token


async def get_current_principal(token: str = Depends(get_token)) -> Principal:
    return await verify_supabase_token(token, get_settings())


async def require_job_secret(x_job_secret: str = Header(default="")) -> None:
    settings: Settings = get_settings()
    if not hmac.compare_digest(x_job_secret.encode(), settings.job_run_secret.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid job secret")
    if settings.is_production and settings.job_run_secret == "change-me-local":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "JOB_RUN_SECRET must be rotated before production")
