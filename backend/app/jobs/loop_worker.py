"""Local always-on worker (Layer 1 convenience ONLY).

In Layer 2 there is no always-on worker: cron-job.org triggers /jobs/run.
This loop exists so development exercises the exact same run_batch() code
path continuously. On the Contabo VPS (Layer 3) this becomes a systemd
service — same handler registry, same queue, same semantics.
"""

import asyncio
import logging
import os

from app.core.config import get_settings
from app.jobs.queue import PostgresJobQueue
from app.jobs.runner import run_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("caios.worker")


async def main() -> None:
    settings = get_settings()
    queue = PostgresJobQueue(settings)
    interval = int(os.environ.get("WORKER_POLL_SECONDS", "5"))
    logger.info("worker started (poll every %ss)", interval)
    try:
        while True:
            report = await run_batch(settings, queue, time_budget_seconds=30)
            if report.claimed:
                logger.info("batch: %s", report.as_dict())
            await asyncio.sleep(interval)
    finally:
        queue.close()


if __name__ == "__main__":
    asyncio.run(main())
