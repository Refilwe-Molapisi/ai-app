"""Authentication — Supabase JWT verification (ADR-002, CAIOS-MASTER-001 §16).

Authentication is Supabase's job; authorisation stays in the backend: a valid
token proves identity only. Business permissions (roles, client isolation,
engagement access) are enforced by application dependencies in api/deps.py —
never by what the UI hides.
"""

import time
from dataclasses import dataclass

import jwt
import httpx
from fastapi import Header, HTTPException, status

from app.core.config import Settings, get_settings

_JWKS_TTL_SECONDS = 3600
_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}


@dataclass(frozen=True)
class Principal:
    subject: str          # Supabase user id (sub)
    email: str | None
    app_role: str | None  # from the custom claim set by the company (16.3)
    client_org_id: str | None  # present for Client Users (16.4)


async def _load_jwks(settings: Settings) -> dict:
    if settings.supabase_jwks_url:
        url = settings.supabase_jwks_url
    elif settings.supabase_url:
        url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    else:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Auth not configured: set SUPABASE_URL or SUPABASE_JWKS_URL",
        )
    now = time.time()
    if _jwks_cache["keys"] is None or now - _jwks_cache["fetched_at"] > _JWKS_TTL_SECONDS:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            _jwks_cache["keys"] = resp.json()
        _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]


async def verify_supabase_token(token: str, settings: Settings | None = None) -> Principal:
    """Verify a Supabase access token and return the principal.

    Development exception: when no auth provider is configured AND the env is
    not production, requests may run unauthenticated so the skeleton is testable
    end-to-end. This shortcut is removed the moment SUPABASE_URL is set.
    """
    settings = settings or get_settings()
    if not (settings.supabase_url or settings.supabase_jwks_url):
        if settings.is_production:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth not configured")
        return Principal(subject="dev-anonymous", email=None, app_role=None, client_org_id=None)

    jwks = await _load_jwks(settings)
    try:
        header = jwt.get_unverified_header(token)
        key = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
        claims = jwt.decode(
            token,
            key=key,
            algorithms=[header.get("alg", "RS256")],
            audience=settings.supabase_audience,
            options={"require": ["exp", "sub"]},
        )
    except Exception as exc:  # noqa: BLE001 — token problems are uniformly 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc

    app_meta = claims.get("app_metadata") or {}
    return Principal(
        subject=claims["sub"],
        email=claims.get("email"),
        app_role=app_meta.get("role"),
        client_org_id=app_meta.get("client_org_id"),
    )


async def get_token(authorization: str = Header(default="")) -> str:
    if authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
