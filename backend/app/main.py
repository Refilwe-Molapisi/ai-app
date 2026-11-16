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
from app.knowledge.routes import router as knowledge_router
from app.jobs.queue import QueueUnavailable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("caios")

_SCHEMA_PATHS = {
    "jobs": Path(__file__).resolve().parent / "jobs" / "schema.sql",
    "knowledge": Path(__file__).resolve().parent / "knowledge" / "schema.sql",
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
        version="0.2.0",
        description="Company AI Operations System — Phases 0–4 foundations "
                    "+ Phase 2 knowledge & document ingestion (§7, §9)",
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
    allowed = [o for o in [
        "http://localhost:3000", "http://127.0.0.1:3000",
    ] if not settings.is_production]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Job-Secret"],
    )
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(maintenance.router)
    app.include_router(knowledge_router)
    return app


app = create_app()
