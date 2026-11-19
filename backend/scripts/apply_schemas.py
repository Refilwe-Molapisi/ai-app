#!/usr/bin/env python3
"""CAIOS schema migration runner (CAIOS-DEPLOY-001).

Applies every module's schema.sql against DATABASE_URL, in dependency order.
Idempotent: every file uses CREATE TABLE IF NOT EXISTS / CREATE INDEX IF
NOT EXISTS, so re-running is always safe (never destructive — column
additions are added as new migration files per master spec §6.4).

Usage:
    python scripts/apply_schemas.py                 # uses $DATABASE_URL
    python scripts/apply_schemas.py --url <dsn>     # explicit override

Production (Supabase): use the *pooler* connection string (port 6543) and
append ?sslmode=require if it is not already present. Run this once after
creating the project (and after enabling the pgvector extension), and again
after every pull that adds a schema or migration.
"""

import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
# Dependency order: the job queue first (other stores borrow its error
# translation), then the modules in the order the phases built them.
SCHEMA_ORDER = (
    "jobs", "knowledge", "ai", "coordination", "deliverables", "ops",
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply all CAIOS schemas (idempotent)")
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL", ""),
                    help="Postgres DSN (default: $DATABASE_URL)")
    args = ap.parse_args()
    if not args.url:
        print("ERROR: no DATABASE_URL (set it or pass --url)", file=sys.stderr)
        return 2

    import psycopg

    applied = 0
    try:
        with psycopg.connect(args.url, autocommit=True) as conn:
            # pgvector backs the knowledge module's embedding column; Supabase
            # enables it via Database → Extensions, raw Postgres via this line.
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            print("extension: vector (ensured)")
            for name in SCHEMA_ORDER:
                sql = (BACKEND / "app" / name / "schema.sql").read_text()
                conn.execute(sql)
                print(f"applied: app/{name}/schema.sql")
                applied += 1
    except Exception as exc:  # noqa: BLE001 — report plainly, exit nonzero
        print(f"ERROR after {applied} schema(s): {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    print(f"OK — {applied} schemas applied against "
          f"{args.url.split('@')[-1].split('/')[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
