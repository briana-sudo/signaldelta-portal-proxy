"""Functional tests for SM_ProxyFront — the graceful-restart reverse proxy.

Covers the three behaviors that make it a fix and not a new failure mode:
  1. transparent passthrough (method, path, body, status, headers, real client IP),
  2. HOLD-across-restart: a request during an upstream outage is retried and served
     when the upstream returns (no 502),
  3. honest 503 (not 502, not a fake 200) when the upstream stays down past budget.
"""
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sm_proxy_front as F


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _echo(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        payload = (b'{"method":"%s","path":"%s","xff":"%s","body":%d}'
                   % (self.command.encode(), self.path.encode(),
                      (self.headers.get("X-Forwarded-For") or "").encode(), len(body)))
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream", "yes")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _echo
    do_POST = _echo


class FrontTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.front_port = _free_port()
        cls.app_port = _free_port()
        # point the front's module globals at our test ports + a short budget
        F.FRONT_PORT = cls.front_port
        F.APP_PORT = cls.app_port
        F.RETRY_BUDGET_S = 3.0
        F.RETRY_BACKOFF_S = 0.1
        cls.front = ThreadingHTTPServer(("127.0.0.1", cls.front_port), F.Handler)
        cls.front.daemon_threads = True
        threading.Thread(target=cls.front.serve_forever, daemon=True).start()
        cls._up = None

    @classmethod
    def tearDownClass(cls):
        cls.front.shutdown()
        if cls._up:
            cls._up.shutdown()

    def _start_upstream(self):
        up = ThreadingHTTPServer(("127.0.0.1", self.app_port), _Upstream)
        up.daemon_threads = True
        threading.Thread(target=up.serve_forever, daemon=True).start()
        type(self)._up = up
        return up

    def _get(self, path, timeout=10):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.front_port}{path}", timeout=timeout)

    def test_1_front_health_no_upstream(self):
        r = self._get("/_front/health")
        self.assertEqual(r.status, 200)
        self.assertIn(b"SM_ProxyFront", r.read())

    def test_2_passthrough_get_and_post(self):
        self._start_upstream()
        time.sleep(0.2)
        r = self._get("/sm/proxy/status")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("X-Upstream"), "yes")     # upstream header relayed
        self.assertIn(b'"path":"/sm/proxy/status"', r.read())
        # POST body + status code passthrough
        req = urllib.request.Request(f"http://127.0.0.1:{self.front_port}/sm/query",
                                     data=b'{"cypher":"x"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        r2 = urllib.request.urlopen(req, timeout=10)
        self.assertEqual(r2.status, 201)
        self.assertIn(b'"body":14', r2.read())                   # body length forwarded intact

    def test_3_hold_across_restart(self):
        """Upstream DOWN, then comes up mid-request → the front holds and serves it
        (no 502). This is the whole point: a deploy-window request survives."""
        if type(self)._up:
            type(self)._up.shutdown()
            type(self)._up = None
            time.sleep(0.2)
        # bring the upstream back after ~1s, while a request is being held
        def delayed_up():
            time.sleep(1.0)
            self._start_upstream()
        threading.Thread(target=delayed_up, daemon=True).start()
        t0 = time.monotonic()
        r = self._get("/sm/proxy/status", timeout=10)            # should NOT 502
        self.assertEqual(r.status, 200)
        self.assertGreater(time.monotonic() - t0, 0.8)           # it actually waited

    def test_4_honest_503_when_upstream_stays_down(self):
        if type(self)._up:
            type(self)._up.shutdown()
            type(self)._up = None
            time.sleep(0.2)
        try:
            self._get("/sm/proxy/status", timeout=10)
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)                        # 503, never a raw 502
            self.assertEqual(e.headers.get("Retry-After"), "2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
