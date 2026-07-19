#!/usr/bin/env python3
"""Serve the wave sidecar with ON-DEMAND snapshots (replaces the 15s loop).

The original `make wave-watch` ran `wave_status.py` every 15s all day, which
burned GitHub GraphQL quota (5000/hr) whether or not anyone had the page open.
This server flips the sidecar from push to pull: a snapshot runs only when the
view asks for one — on page open and on its Refresh button.

One process does both jobs the old two-command flow split:
  - static file serving for this directory (wave_status.html, the JSONs,
    code_health.html), and
  - `GET /snapshot` → runs `wave_status.py` once, then returns
    `{"ran": bool, "ok": bool, "detail": str}`. Back-to-back requests inside
    the debounce window (two tabs, a double-click) reuse the fresh file
    instead of re-hitting the GitHub API.

Run: `make wave-watch` (or `python3 scripts/wave_serve.py [--port 8777]`),
then open http://localhost:8777/wave_status.html. Stdlib only, like the rest
of the sidecar. `--selftest` checks the pure debounce/response helpers.
"""

from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8777
DEBOUNCE_S = 5.0
SNAPSHOT_TIMEOUT_S = 120

_lock = threading.Lock()
_last_run_at: float | None = None


def should_run(last_run_at: float | None, now: float, debounce_s: float = DEBOUNCE_S) -> bool:
    """Pure debounce decision: run unless a snapshot finished < debounce_s ago."""
    return last_run_at is None or (now - last_run_at) >= debounce_s


def snapshot_response(ran: bool, ok: bool, detail: str) -> dict:
    """The /snapshot JSON body (stable shape; the view only branches on `ok`)."""
    return {"ran": ran, "ok": ok, "detail": detail}


def run_snapshot() -> dict:
    """Run wave_status.py once, serialised + debounced across concurrent requests."""
    global _last_run_at
    with _lock:
        if not should_run(_last_run_at, time.monotonic()):
            return snapshot_response(False, True, "debounced — reusing the fresh snapshot")
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "wave_status.py")],
                capture_output=True,
                text=True,
                timeout=SNAPSHOT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return snapshot_response(True, False, f"wave_status.py timed out ({SNAPSHOT_TIMEOUT_S}s)")
        _last_run_at = time.monotonic()
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return snapshot_response(True, False, " / ".join(tail) or f"exit {proc.returncode}")
        return snapshot_response(True, True, "snapshot written")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPTS_DIR), **kwargs)

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path.partition("?")[0] == "/snapshot":
            body = json.dumps(run_snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # The view cache-busts with a query param, but belt-and-braces the JSONs.
        if self.path.partition("?")[0].endswith(".json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):  # quieter: one line per snapshot only
        if "/snapshot" in fmt % args:
            sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def selftest() -> None:
    assert should_run(None, 0.0)
    assert not should_run(100.0, 104.9)
    assert should_run(100.0, 105.0)
    assert should_run(100.0, 200.0, debounce_s=50.0)
    assert not should_run(100.0, 149.9, debounce_s=50.0)
    resp = snapshot_response(True, False, "boom")
    assert set(resp) == {"ran", "ok", "detail"} and resp["ok"] is False
    print("wave_serve selftest OK (debounce + response shape)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"wave sidecar: http://localhost:{args.port}/wave_status.html  (Ctrl-C to stop)")
    print("snapshots run on page open / Refresh only — no background polling")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
