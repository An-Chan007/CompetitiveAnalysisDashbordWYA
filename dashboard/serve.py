#!/usr/bin/env python3
"""Serve the dashboard AND allow refreshing it from the browser. Stdlib only.

Replaces `python3 -m http.server`. Same static serving, plus two endpoints the
Refresh button uses:

    POST /api/refresh   start a collect + digest run (409 if one is running)
    GET  /api/status    {running, ok, started, finished, log: [...]}

The dashboard feature-detects /api/status. Served by plain http.server instead,
the button hides itself, so nothing breaks.

Binds to 127.0.0.1 only - this runs a subprocess on POST and must not be
reachable from the network. No request data reaches the subprocess: the commands
are fixed constants.

Usage:
    python3 serve.py            # port 8080
    python3 serve.py 9000
"""

import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent

# Fixed. Never built from request input.
STEPS = [
    ("collect", [sys.executable, "collect.py"]),
    ("digest",  [sys.executable, "digest.py", "--days=7"]),
    ("briefs",  [sys.executable, "brief.py", "--all"]),
]

STATE = {"running": False, "ok": None, "started": None, "finished": None,
         "log": [], "step": None}
LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_pipeline():
    with LOCK:
        STATE.update(running=True, ok=None, started=now(), finished=None,
                     log=["starting refresh"], step=None)
    ok = True
    try:
        for name, cmd in STEPS:
            with LOCK:
                STATE["step"] = name
                STATE["log"].append(f"--- {name} ---")
            proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                with LOCK:
                    STATE["log"].append(line)
                    # keep memory bounded on a long collect
                    if len(STATE["log"]) > 400:
                        del STATE["log"][:200]
            if proc.wait() != 0:
                ok = False
                with LOCK:
                    STATE["log"].append(f"{name} failed (exit {proc.returncode})")
                break
    except OSError as e:
        ok = False
        with LOCK:
            STATE["log"].append(f"could not run: {e}")
    with LOCK:
        STATE.update(running=False, ok=ok, finished=now(), step=None)
        STATE["log"].append("done" if ok else "finished with errors")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            with LOCK:
                return self._json(200, dict(STATE))
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/refresh":
            return self._json(404, {"error": "not found"})
        with LOCK:
            if STATE["running"]:
                return self._json(409, {"error": "a refresh is already running",
                                        "started": STATE["started"]})
        threading.Thread(target=run_pipeline, daemon=True).start()
        return self._json(202, {"started": True})

    def end_headers(self):
        # data files change under us; never let a browser cache them
        if self.path.endswith((".json", ".jsonl", ".html")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "/api/" in self.path:      # keep the console readable
            return
        super().log_message(fmt, *args)


def selftest():
    """No network, no server. Checks the wiring that is easy to get wrong."""
    assert all(c[0] == sys.executable for _, c in STEPS), \
        "steps must use the running interpreter, not a bare 'python3'"
    for _, cmd in STEPS:
        assert (HERE / cmd[1]).exists(), f"missing script: {cmd[1]}"
        for part in cmd:
            assert not any(ch in part for ch in ";|&$`"), "shell metachar in cmd"
    assert STATE["running"] is False
    assert Handler.__init__.__doc__ is None      # sanity: subclass intact
    print("serve selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        selftest()
        raise SystemExit
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8080)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"WYA market monitor -> http://localhost:{port}")
    print("Refresh button is live. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
