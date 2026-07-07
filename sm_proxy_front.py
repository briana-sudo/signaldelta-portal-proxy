"""SM_ProxyFront — a tiny, always-up reverse proxy that fronts the FastAPI app.

WHY THIS EXISTS
    Every deploy bounces SignalDeltaProxy with a hard `nssm restart` (stop -> ~3-5s
    gap -> start). During that gap nothing listens on the app port, so cloudflared
    gets connection-refused on the origin and Cloudflare's edge returns **502 Bad
    Gateway** to the browser. The trading dashboard reads (7687) and the /sm/*
    console BOTH flow through the same app process, so a single bounce 502s both.

    This front sits on the public origin port (:8000, where cloudflared points) and
    upstreams to the real app on :8001. When the app is bounced, the front stays up
    and RETRIES the upstream for a short window — so a request that arrives during a
    restart is held and served the moment the app returns, instead of surfacing as a
    502. If the app stays down past the window, the front returns an honest, RETRYABLE
    503 (never a raw 502, never a fake success).

SCOPE / FIREWALL (mirrors SM_ProxyHelper)
    Binds 127.0.0.1 only. Holds NO 7688/7687 driver, reads no research state, has NO
    trade path, makes no auth decision (it forwards Authorization untouched; the app
    still enforces require_operator_identity). Stdlib only — fast start, no venv
    coupling, so it is itself near-never-restarted.

POST-SAFETY
    A restart-window failure is retried ONLY when the upstream connection was REFUSED
    (no bytes were sent, so no request was executed — retry cannot double-apply). If a
    connection is established and then drops mid-exchange, we do NOT retry (that could
    double-execute a POST); we surface an honest 502 for that rare case.
"""
from __future__ import annotations

import http.client
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRONT_HOST = os.environ.get("SM_FRONT_HOST", "127.0.0.1")
FRONT_PORT = int(os.environ.get("SM_FRONT_PORT", "8000"))
APP_HOST = os.environ.get("SM_APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("SM_APP_PORT", "8001"))

# Restart-window budget: how long the front holds a request while the app is being
# bounced before giving up with a retryable 503. A hard nssm restart is ~3-5s; 12s
# covers a slow disk/venv start with margin. Backoff between upstream attempts.
RETRY_BUDGET_S = float(os.environ.get("SM_FRONT_RETRY_BUDGET_S", "12"))
RETRY_BACKOFF_S = float(os.environ.get("SM_FRONT_RETRY_BACKOFF_S", "0.15"))
UPSTREAM_TIMEOUT_S = float(os.environ.get("SM_FRONT_UPSTREAM_TIMEOUT_S", "60"))

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1). We manage framing
# ourselves (fixed Content-Length), so drop Transfer-Encoding too.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _log(msg: str) -> None:
    """Timestamped line to stdout (nssm captures it to the front's log). Quiet by
    default — only lifecycle + restart-window events, never per-request noise.
    ASCII-only + crash-proof: nssm's stdout is cp1252 on Windows, so a stray non-ASCII
    char would raise UnicodeEncodeError and take down the request — logging must never
    do that."""
    try:
        print(f"[sm-front] {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}", flush=True)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "SMProxyFront/1.0"
    protocol_version = "HTTP/1.1"

    # silence per-request access logging (the APP already writes the access log)
    def log_message(self, *a):  # noqa: D401
        return

    # ---- own liveness (distinct from the app's /health) ---------------------
    def _front_health(self) -> None:
        body = b'{"status":"ok","service":"SM_ProxyFront","upstream":"%d"}' % APP_PORT
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _forward_headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in self.headers.items():
            if k.lower() in _HOP_BY_HOP or k.lower() == "host":
                continue
            out[k] = v
        # preserve the real client IP for the app's access log + restart provenance.
        # cloudflared sets X-Forwarded-For; append our peer only if absent.
        if "X-Forwarded-For" not in {k for k in out}:
            peer = self.client_address[0] if self.client_address else "127.0.0.1"
            out["X-Forwarded-For"] = peer
        return out

    def _attempt_once(self, method: str, path: str, headers: dict[str, str], body: bytes):
        """One upstream attempt. Returns the http.client response on success, or
        raises. ConnectionRefusedError specifically means the app was not listening
        (safe to retry — no request was delivered)."""
        conn = http.client.HTTPConnection(APP_HOST, APP_PORT, timeout=UPSTREAM_TIMEOUT_S)
        conn.request(method, path, body=body, headers=headers)
        return conn, conn.getresponse()

    def _relay(self) -> None:
        method = self.command
        path = self.path

        if path.rstrip("/") == "/_front/health":
            return self._front_health()

        body = self._read_body()
        headers = self._forward_headers()

        deadline = time.monotonic() + RETRY_BUDGET_S
        waited_for_restart = False
        conn = resp = None
        while True:
            try:
                conn, resp = self._attempt_once(method, path, headers, body)
                break
            except (ConnectionRefusedError, socket.timeout, TimeoutError) as e:
                # app not listening yet (mid-restart) OR connect timed out before any
                # bytes were sent → safe to retry within the budget.
                if time.monotonic() >= deadline:
                    if waited_for_restart:
                        _log(f"upstream still down after {RETRY_BUDGET_S:.0f}s -> 503 for {method} {path}")
                    return self._send_simple(
                        503,
                        b'{"error":"origin restarting","hint":"the proxy is cycling; retry shortly"}',
                        extra={"Retry-After": "2"},
                    )
                if not waited_for_restart:
                    _log(f"upstream refused ({type(e).__name__}) - holding {method} {path} across restart")
                    waited_for_restart = True
                time.sleep(RETRY_BACKOFF_S)
                continue
            except (ConnectionResetError, http.client.HTTPException, OSError) as e:
                # connection was established then failed mid-exchange — NOT safe to
                # retry (a POST may have partially executed). Honest 502, no retry.
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                return self._send_simple(
                    502,
                    b'{"error":"bad gateway","detail":"upstream failed mid-response"}',
                )

        if waited_for_restart:
            _log(f"upstream recovered - served {method} {path}")

        try:
            data = resp.read()
            self.send_response(resp.status)
            sent_len = False
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in _HOP_BY_HOP:
                    continue
                if lk == "content-length":
                    sent_len = True
                self.send_header(k, v)
            if not sent_len:
                self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _send_simple(self, code: int, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # every method funnels through _relay
    do_GET = _relay
    do_POST = _relay
    do_PUT = _relay
    do_DELETE = _relay
    do_PATCH = _relay
    do_OPTIONS = _relay
    do_HEAD = _relay


def main() -> None:
    _log(f"starting on {FRONT_HOST}:{FRONT_PORT} -> app {APP_HOST}:{APP_PORT} "
         f"(retry budget {RETRY_BUDGET_S:.0f}s)")
    httpd = ThreadingHTTPServer((FRONT_HOST, FRONT_PORT), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
