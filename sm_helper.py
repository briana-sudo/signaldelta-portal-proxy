"""SM_ProxyHelper — a tiny always-on service whose ONLY job is to restart (or start)
the SignalDeltaProxy Windows service on request.

Why it exists: the proxy's own /sm/proxy/restart lives INSIDE the proxy, so it can't
help if the proxy is down or running code that lacks the endpoint. This helper runs
as its OWN service (SYSTEM, auto-start), OUTSIDE the proxy's process tree, so it can
cycle the proxy in any state — making the portal's "Restart proxy" button work even
for the first restart after a proxy code change.

Scope is deliberately one thing: restart/start the proxy service. It binds
127.0.0.1 only, is token-gated, holds NO 7688/7687 driver, reads no research state,
and has NO trade path. Stdlib only (fast start, no venv coupling).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_SERVICE = os.environ.get("SM_PROXY_SERVICE", "SignalDeltaProxy")
HELPER_HOST = os.environ.get("SM_HELPER_HOST", "127.0.0.1")
HELPER_PORT = int(os.environ.get("SM_HELPER_PORT", "8199"))
HELPER_TOKEN = os.environ.get("SM_HELPER_TOKEN")            # shared with the proxy; set by setup
NSSM = os.environ.get("SM_NSSM_PATH", r"C:\SignalDelta_Local\tools\nssm.exe")
_TIMEOUT = 25


def _log(msg: str) -> None:
    """Timestamped line to stdout (nssm captures it to sm_helper.log). The helper used
    to log NOTHING, so a restart driven through it left no actor trace anywhere — this
    closes that blind spot. Never logs the token, only a short non-reversible
    fingerprint of it, so the log is safe to read. ASCII-only + crash-proof: nssm's
    stdout is cp1252, so logging must never raise UnicodeEncodeError on a stray char."""
    try:
        print(f"[sm-helper] {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}", flush=True)
    except Exception:
        pass


def _tok_fingerprint(raw: str | None) -> str:
    """First 8 hex of sha256(token) — proves WHICH credential called without ever
    logging the credential. 'none' if unauthenticated (localhost-trust mode)."""
    if not raw:
        return "none"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _sc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=_TIMEOUT)


def classify_state(text: str) -> str:
    if "1060" in text or "does not exist" in text:
        return "not-installed"
    if "START_PENDING" in text:
        return "starting"
    if "STOP_PENDING" in text:
        return "stopping"
    if "RUNNING" in text:
        return "running"
    if "STOPPED" in text:
        return "stopped"
    return "unknown"


def proxy_status() -> str:
    try:
        out = _sc("query", PROXY_SERVICE)
    except Exception:
        return "unknown"
    return classify_state((out.stdout or "") + (out.stderr or ""))


def _do_restart() -> None:
    """Cycle the proxy service. Runs in a background thread so the HTTP response
    returns first; a brief pause lets the response flush before the proxy drops."""
    time.sleep(1.0)
    try:
        done = False
        if os.path.exists(NSSM):
            r = subprocess.run([NSSM, "restart", PROXY_SERVICE], capture_output=True, text=True, timeout=60)
            done = (r.returncode == 0)
            _log(f"restart dispatched via nssm returncode={r.returncode}")
        if not done:                                   # nssm unavailable/unprivileged → SCM stop+start
            _log("nssm restart unavailable/failed — falling back to sc stop+start")
            _sc("stop", PROXY_SERVICE)
            time.sleep(3)
            _sc("start", PROXY_SERVICE)
            _log("restart dispatched via sc stop+start")
    except Exception as e:
        _log(f"restart dispatch RAISED: {str(e)[:160]}")


def _do_start() -> None:
    try:
        _sc("start", PROXY_SERVICE)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "SMProxyHelper/1.0"

    def _authed(self) -> bool:
        if not HELPER_TOKEN:                     # no token configured → localhost-only trust
            return True
        tok = self.headers.get("X-Helper-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            tok = tok or auth[7:]
        return tok == HELPER_TOKEN

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                   # quiet (no request logging noise)
        return

    def do_GET(self):
        if self.path.rstrip("/") == "/helper/health":
            return self._send(200, {"ok": True, "service": "SM_ProxyHelper"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path.rstrip("/") == "/helper/status":
            return self._send(200, {"status": proxy_status(), "proxy_service": PROXY_SERVICE})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        p = self.path.rstrip("/")
        caller = self.client_address[0] if self.client_address else "?"
        fp = _tok_fingerprint(
            self.headers.get("X-Helper-Token")
            or ((self.headers.get("Authorization") or "")[7:] or None))
        if p == "/helper/restart":
            _log(f"ACCEPTED /helper/restart from {caller} token={fp} -> cycling {PROXY_SERVICE}")
            threading.Thread(target=_do_restart, daemon=True).start()
            return self._send(202, {"action": "restart", "status": "restarting", "by": "SM_ProxyHelper"})
        if p == "/helper/start":
            _log(f"ACCEPTED /helper/start from {caller} token={fp} -> starting {PROXY_SERVICE}")
            threading.Thread(target=_do_start, daemon=True).start()
            return self._send(202, {"action": "start", "status": "starting", "by": "SM_ProxyHelper"})
        return self._send(404, {"error": "not found"})


def main() -> None:
    httpd = ThreadingHTTPServer((HELPER_HOST, HELPER_PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
