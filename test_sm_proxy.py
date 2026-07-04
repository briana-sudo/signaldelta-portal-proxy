"""Phase 3d-iii-a — search-master proxy tests (stdlib unittest; no live 7688/secrets).

Run:  .venv/Scripts/python -m unittest test_sm_proxy -v

Covers: the WHITELIST allowlist (admit read-shaped, reject write/unparseable/comment-
hidden/casing/multi-statement/non-read-CALL); auth-off-the-client (identity header
required); allowlist-runs-before-the-driver; 7688 isolation (no 7687 reference,
SM pool is 7688-only — structural); the server-side secrets store (never returns/
echoes a value; onboarding response carries no credential).
"""
from __future__ import annotations

import unittest

from fastapi import HTTPException

import sm_proxy
from sm_cypher_allowlist import is_read_shaped
from sm_proxy import (
    SM_NEO4J_URI, SMOnboardRequest, require_operator_identity, sm_onboard,
)
from sm_secrets import SecretsStore


class TestAllowlistAdmitsReadShaped(unittest.TestCase):
    def test_admits(self):
        for q in [
            "MATCH (n) RETURN n",
            "MATCH (n:SMBoardItem) WHERE n.lane = $lane RETURN n.id AS id ORDER BY id LIMIT 10",
            "OPTIONAL MATCH (n)-[:SM_HAS]->(m) RETURN n, m",
            "RETURN 1 AS one",
            "WITH 1 AS x RETURN x",
            "UNWIND $ids AS i MATCH (n {id: i}) RETURN n",
            "CALL db.labels()",
            "CALL { MATCH (n) RETURN n } RETURN 1",
        ]:
            with self.subTest(q=q):
                self.assertTrue(is_read_shaped(q).allowed, is_read_shaped(q).reason)


class TestAllowlistRejectsEverythingElse(unittest.TestCase):
    def test_rejects(self):
        for q in [
            "CREATE (n)",
            "MATCH (n) SET n.x = 1 RETURN n",
            "MATCH (n) DELETE n",
            "MATCH (n) DETACH DELETE n",
            "MERGE (n {id: 1}) RETURN n",
            "MATCH (n) REMOVE n.x RETURN n",
            "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {})",
            "CALL apoc.create.node(['X'], {})",
            "MATCH (n) RETURN n ; CREATE (m)",          # second statement
            "// harmless\nCREATE (n)",                   # comment cannot hide a write
            "cReAtE (n)",                                # casing trick
            "MATCH (((",                                 # unparseable
            "",                                          # empty
            "FOREACH (x IN [1] | CREATE (:N))",
            "LOAD CSV FROM 'file:///x' AS r CREATE (:N)",
        ]:
            with self.subTest(q=q):
                self.assertFalse(is_read_shaped(q).allowed, f"should reject: {q!r}")

    def test_reject_reasons_are_populated(self):
        v = is_read_shaped("MATCH (n) SET n.x=1 RETURN n")
        self.assertFalse(v.allowed)
        self.assertIn("allowlist", v.reason.lower())


class TestAuthOffTheClient(unittest.TestCase):
    def test_missing_identity_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            require_operator_identity(None)
        self.assertEqual(cm.exception.status_code, 401)

    def test_present_identity_returned(self):
        self.assertEqual(require_operator_identity("brian@kaskaskia.com"), "brian@kaskaskia.com")

    def test_bearer_token_fallback_when_no_cloudflare_access(self):
        import os
        os.environ["PROXY_API_TOKEN"] = "tok123"
        try:
            self.assertEqual(require_operator_identity(None, "Bearer tok123"), "operator@bearer")
            with self.assertRaises(HTTPException):
                require_operator_identity(None, "Bearer wrong")   # invalid token still rejected
        finally:
            del os.environ["PROXY_API_TOKEN"]


class TestAllowlistRunsBeforeTheDriver(unittest.TestCase):
    def test_write_rejected_by_allowlist_not_the_db(self):
        # a write is rejected at the allowlist (400) — it never reaches a driver,
        # so this passes with NO live 7688
        with self.assertRaises(HTTPException) as cm:
            sm_proxy._read_query("MATCH (n) DELETE n", {}, source="test")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("allowlist", cm.exception.detail.lower())

    def test_read_shaped_reaches_driver_gate_503_when_7688_absent(self):
        # a read-shaped query passes the allowlist and reaches get_sm_driver, which
        # 503s until 7688 is provisioned — proving the ORDER (allowlist first, then
        # the 7688-only driver), still with no live DB
        sm_proxy._sm_driver = None
        old = sm_proxy.SM_NEO4J_PASSWORD
        sm_proxy.SM_NEO4J_PASSWORD = None
        try:
            with self.assertRaises(HTTPException) as cm:
                sm_proxy._read_query("MATCH (n) RETURN n", {}, source="test")
            self.assertEqual(cm.exception.status_code, 503)
        finally:
            sm_proxy.SM_NEO4J_PASSWORD = old

    def test_rejection_is_logged_as_drift(self):
        before = len(sm_proxy.rejection_log())
        try:
            sm_proxy._read_query("CREATE (n)", {}, source="test")
        except HTTPException:
            pass
        after = sm_proxy.rejection_log()
        self.assertEqual(len(after), before + 1)
        self.assertEqual(after[-1]["drift"], "read-path-write-attempt")


class TestInstanceIsolation(unittest.TestCase):
    def test_sm_pool_is_7688_only_no_7687_reference(self):
        import inspect
        src = inspect.getsource(sm_proxy)
        self.assertNotIn("7687", src, "search-master proxy must hold no 7687 reference (§6)")
        self.assertNotIn("bolt://localhost:7687", src)
        self.assertTrue(SM_NEO4J_URI.endswith(":7688"), SM_NEO4J_URI)

    def test_no_trading_engine_labels_leaked(self):
        import inspect
        src = inspect.getsource(sm_proxy)
        # branch-isolation backstop references KCC/KTM only as an EXCLUSION filter
        self.assertIn("NOT n:KCCNode AND NOT n:KTMNode", src)


class TestEnginePowerSwitch(unittest.TestCase):
    def test_state_classifier_maps_sc_output(self):
        from sm_engine import classify_state
        self.assertEqual(classify_state("STATE : 4  RUNNING"), "running")
        self.assertEqual(classify_state("STATE : 1  STOPPED"), "stopped")
        self.assertEqual(classify_state("STATE : 2  START_PENDING"), "starting")
        self.assertEqual(classify_state("STATE : 3  STOP_PENDING"), "stopping")
        self.assertEqual(classify_state("The specified service does not exist (1060)"), "not-installed")

    def test_control_endpoints_registered(self):
        paths = {r.path for r in sm_proxy.sm_router.routes}
        self.assertIn("/sm/engine/status", paths)
        self.assertIn("/sm/engine/start", paths)
        self.assertIn("/sm/engine/stop", paths)

    def test_engine_module_is_a_power_switch_no_research_or_trading_path(self):
        import inspect, sm_engine
        src = inspect.getsource(sm_engine)
        # power switch only: service control (sc.exe), never a DB/research/trading path
        for forbidden in ("7687", "TradeNode", "neo4j", "GraphDatabase", "cypher", "ResolveAPI"):
            self.assertNotIn(forbidden, src, f"engine power switch must not reference {forbidden!r}")
        self.assertIn("sc.exe", src)                  # it IS just the service controller


class TestServerSideSecrets(unittest.TestCase):
    def test_store_never_returns_value_publicly(self):
        s = SecretsStore()
        s.set("options-feed_api_key", "sk-SECRET-123")
        self.assertTrue(s.configured("options-feed_api_key"))
        # no public accessor returns the value; only the underscore-private _use does
        self.assertFalse(any(n for n in dir(s) if not n.startswith("_") and "get" in n.lower()))
        self.assertNotIn("sk-SECRET-123", repr(s))          # never echoed in logs

    def test_onboard_response_carries_no_credential(self):
        secret = "sk-DO-NOT-LEAK-999"
        resp = sm_onboard(SMOnboardRequest(source_id="options-feed", entitlement="opra_options",
                                           credential=secret, watermark="2026-09-01", content_hash="abc"))
        self.assertTrue(resp["configured"])
        self.assertNotIn(secret, repr(resp))                # credential never returned
        self.assertNotIn("credential", resp)                # record shows configured, not the key


if __name__ == "__main__":
    unittest.main(verbosity=2)
