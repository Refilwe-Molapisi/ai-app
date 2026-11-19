"""CAIOS API entrypoint — modular monolith (§5.2).

Modules are separated logically (routers/adapters/jobs) precisely so they can
be split into services later if justified — but ship as one deployable until
then. The same image serves Layer 1 (local Docker), Layer 2 (Render free) and
Layer 3 (Contabo VPS); only environment variables differ.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.api.routes import health, jobs, maintenance
from app.agents.routes import router as agents_router
from app.coordination.routes import router as coordination_router
from app.deliverables.routes import router as deliverables_router
from app.knowledge.routes import router as knowledge_router
from app.ai.routes import router as ai_router, prompt_router as ai_prompt_router
from app.jobs.queue import QueueUnavailable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("caios")

_SCHEMA_PATHS = {
    "jobs": Path(__file__).resolve().parent / "jobs" / "schema.sql",
    "knowledge": Path(__file__).resolve().parent / "knowledge" / "schema.sql",
    "ai": Path(__file__).resolve().parent / "ai" / "schema.sql",
    "coordination": Path(__file__).resolve().parent / "coordination" / "schema.sql",
    "deliverables": Path(__file__).resolve().parent / "deliverables" / "schema.sql",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("CAIOS API starting — env=%s", settings.caios_env)
    if settings.is_production and not (settings.supabase_url or settings.supabase_jwks_url):
        raise RuntimeError("Production requires configured Supabase auth (§16.1)")
    try:
        from app.jobs.queue import PostgresJobQueue
        queue = PostgresJobQueue(settings)
        for label, path in _SCHEMA_PATHS.items():
            try:
                # Each schema ensures independently: a missing pgvector extension
                # must not stop the queue bootstrap (and vice versa).
                queue.ensure_schema(path.read_text())
                logger.info("%s schema ensured", label)
            except Exception:  # noqa: BLE001
                logger.exception("%s schema bootstrap failed; /health stays available", label)
        queue.close()
    except Exception:  # noqa: BLE001 — API must still boot for /health (keep-alive)
        logger.exception("job queue bootstrap failed; /health stays available")
    yield
    logger.info("CAIOS API shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CAIOS API",
        version="0.5.0",
        description="Company AI Operations System — Phases 0–4 foundations, "
                    "knowledge ingestion (2), AI integration (3), central "
                    "coordination (4) and specialist agents (5)",
        lifespan=lifespan,
    )

    @app.exception_handler(QueueUnavailable)
    async def queue_unavailable(request, exc):  # noqa: ANN001
        """Cold-start safety: DB/queue connection failures surface as clean 503s
        (keep-alive and cron callers retry; jobs stay pending in the queue)."""
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503,
                            content={"detail": "job queue unavailable; retry shortly"})

    # CORS: explicit allow-list; never wildcard when credentials are involved.
    # "null" covers the single-file UI prototype opened from file:// in dev —
    # production origins come from CORS_ORIGINS (e.g. the Vercel domain),
    # configured, never wildcards.
    allowed = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if not settings.is_production:
        allowed += ["http://localhost:3000", "http://127.0.0.1:3000", "null",
                    "http://localhost:8000", "http://127.0.0.1:8000"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "X-Job-Secret"],
    )
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(maintenance.router)
    app.include_router(knowledge_router)
    app.include_router(ai_router)
    app.include_router(ai_prompt_router)
    app.include_router(coordination_router)
    app.include_router(agents_router)
    app.include_router(deliverables_router)
    from app.ops.routes import router as ops_router
    app.include_router(ops_router)
    return app


app = create_app()
