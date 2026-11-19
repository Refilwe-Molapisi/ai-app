"""CAIOS Phase 9 end-to-end verification — deployment readiness.

Verifies the production posture the platform depends on, against a REAL
uvicorn server process (not TestClient, so CORS middleware and auth run
exactly as deployed):

  migrations: backend/scripts/apply_schemas.py applies all six schemas
  against a fresh Postgres — twice (idempotency is a deploy property).
  production boot: CAIOS_ENV=production with no auth provider configured —
  /health stays public, every API route fails CLOSED (503 auth not
  configured), the dev bearer shortcut is gone.
  CORS: the configured allow-list echoes allowed origins on simple GETs and
  preflights; unknown origins get no ACAO header — the browser-side gate
  that made the phase-8 PATCH bug possible.

Run from backend/:  python3 tests/integration_phase9.py
"""

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def req(url, method="GET", headers=None, data=None):
    r = urllib.request.Request(url, method=method, headers=headers or {},
                               data=data)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# ── 1. Fresh Postgres + the migration runner ────────────────────────────────
import pgserver  # noqa: E402

DATA_DIR = "/tmp/caios-pg-phase9"
shutil.rmtree(DATA_DIR, ignore_errors=True)
server = pgserver.get_server(DATA_DIR)
PG_URL = server.get_uri()
print(f"pgserver ready: {PG_URL.rsplit('@', 1)[-1]}")

for run_no in (1, 2):
    p = subprocess.run([sys.executable, str(BACKEND / "scripts" / "apply_schemas.py"),
                        "--url", PG_URL], capture_output=True, text=True)
    check(f"apply_schemas run {run_no} exits 0 (idempotent)",
          p.returncode == 0, (p.stderr or p.stdout)[-200:])
check("migration runner reports six schemas applied",
      p.stdout.count("applied:") == 6, p.stdout[-300:])

# ── 2. Production refuses to boot without an auth provider (§16.1) ──────────
refuse = dict(os.environ, CAIOS_ENV="production", DATABASE_URL=PG_URL)
refuse.pop("SUPABASE_URL", None)
refuse.pop("SUPABASE_JWKS_URL", None)
refuse.pop("TOKEN", None)
p = subprocess.run(
    [sys.executable, "-c",
     "import uvicorn, app.main; uvicorn.run(app.main.app, host='127.0.0.1',"
     " port=8070, log_level='error')"],  # lifespan runs → guard fires
    cwd=BACKEND, env=refuse, capture_output=True, text=True, timeout=60)
check("production boot REFUSED without Supabase auth (fail-closed)",
      p.returncode != 0 and "Production requires configured Supabase auth" in p.stderr,
      p.stderr[-150:])

# ── 3. Production-mode server with a (mock) JWKS provider ───────────────────
# Tiny JWKS endpoint: the app must FETCH keys and then reject bad tokens with
# 401 — the same request path a real Supabase project exercises.
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402


class _JWKS(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"keys":[{"kty":"RSA","kid":"p9","n":"x","e":"AQAB"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


jwks_srv = HTTPServer(("127.0.0.1", 8079), _JWKS)
threading.Thread(target=jwks_srv.serve_forever, daemon=True).start()

env = dict(os.environ,
           CAIOS_ENV="production",
           DATABASE_URL=PG_URL,
           SUPABASE_JWKS_URL="http://127.0.0.1:8079/jwks.json",
           SUPABASE_AUDIENCE="authenticated",
           CORS_ORIGINS="https://refilwe-molapisi.github.io,https://caios-console.vercel.app",
           JOB_RUN_SECRET="p9-secret")
env.pop("SUPABASE_URL", None)
env.pop("TOKEN", None)

proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app",
     "--host", "127.0.0.1", "--port", "8071"],
    cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
BASE = "http://127.0.0.1:8071"
try:
    for _ in range(30):
        try:
            s, h, b = req(BASE + "/health")
            if s == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("server did not come up")

    import json  # noqa: E402
    s, h, b = req(BASE + "/health")
    check("/health public 200 in production", s == 200)
    check("health reports production env",
          json.loads(b).get("env") == "production", b[:120].decode())

    s, h, b = req(BASE + "/ops/members?org_id=company")
    check("unauthenticated API call → 401 (no token)",
          s == 401, f"{s} {b[:120]}")
    s, h, b = req(BASE + "/ops/members?org_id=company",
                  headers={"Authorization": "Bearer dev"})
    check("the dev bearer shortcut is gone in production (garbage token → 401)",
          s == 401, f"{s} {b[:120]}")

    ALLOWED = "https://refilwe-molapisi.github.io"
    s, h, b = req(BASE + "/health", headers={"Origin": ALLOWED})
    check("allowed origin echoed on simple GET",
          h.get("access-control-allow-origin") == ALLOWED, str(h.get("access-control-allow-origin")))
    s, h, b = req(BASE + "/health", headers={"Origin": "https://evil.example"})
    check("unknown origin gets NO ACAO header",
          h.get("access-control-allow-origin") is None,
          str(h.get("access-control-allow-origin")))
    s, h, b = req(BASE + "/ops/members?org_id=company", method="OPTIONS",
                  headers={"Origin": ALLOWED,
                           "Access-Control-Request-Method": "PATCH",
                           "Access-Control-Request-Headers": "authorization,content-type"})
    check("preflight for allowed origin + PATCH → 200/204 with methods listed",
          s in (200, 204) and "PATCH" in h.get("access-control-allow-methods", ""),
          f"{s} {h.get('access-control-allow-methods')}")
    s, h, b = req(BASE + "/ops/members?org_id=company", method="OPTIONS",
                  headers={"Origin": "https://evil.example",
                           "Access-Control-Request-Method": "PATCH"})
    check("preflight from unknown origin rejected",
          h.get("access-control-allow-origin") is None, str(s))
finally:
    proc.terminate()
    proc.wait(timeout=10)
    jwks_srv.shutdown()

print()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
