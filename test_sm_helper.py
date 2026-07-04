"""SM_ProxyHelper — auth gating + restart handoff. Stdlib unittest (runnable with
`python -m unittest test_sm_helper`); no external deps so it runs anywhere the
helper does. The proxy service is never actually restarted here — the restart
worker is stubbed and the target is a fake service name."""
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("SM_PROXY_SERVICE", "SM_NoSuchService_Test")
os.environ["SM_HELPER_PORT"] = "8232"
os.environ["SM_HELPER_TOKEN"] = "unittok"

import sm_helper  # noqa: E402


def _call(path, method="GET", tok=None):
    h = {}
    if tok:
        h["X-Helper-Token"] = tok
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(f"http://127.0.0.1:8232{path}", headers=h, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class HelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.restarts = []
        sm_helper._do_restart = lambda: cls.restarts.append(1)   # stub: never touch a real service
        cls.srv = sm_helper.ThreadingHTTPServer(("127.0.0.1", 8232), sm_helper.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_health_is_open(self):
        code, body = _call("/helper/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_status_requires_token(self):
        self.assertEqual(_call("/helper/status")[0], 401)
        self.assertEqual(_call("/helper/status", tok="unittok")[0], 200)

    def test_restart_requires_token_and_hands_off(self):
        self.assertEqual(_call("/helper/restart", "POST")[0], 401)          # no token
        code, body = _call("/helper/restart", "POST", tok="unittok")
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], "restarting")
        time.sleep(0.1)
        self.assertGreaterEqual(len(self.restarts), 1)                       # restart worker invoked

    def test_no_trade_or_graph_surface(self):
        # code markers (not prose) — the helper has no graph driver and no trade path
        src = open("sm_helper.py", encoding="utf-8").read()
        for forbidden in ("import neo4j", "GraphDatabase", "bolt://", "/v2/orders", "submit_order"):
            self.assertNotIn(forbidden, src)                                 # helper is restart-only


if __name__ == "__main__":
    unittest.main()
