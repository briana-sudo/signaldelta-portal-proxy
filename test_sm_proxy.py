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


class TestOriginDenyByConstruction(unittest.TestCase):
    """PROVENANCE (DEF-019): operator-click is producible ONLY from a real Cloudflare-Access
    identity (a browser-SSO email the tunnel injects + a client cannot forge). A bearer
    caller — a shell OR a token-baked browser — is operator-token, never operator-click."""
    def test_operator_click_only_from_cf_identity(self):
        self.assertEqual(sm_proxy._origin("brian@kaskaskia.com"), "operator-click")
        self.assertEqual(sm_proxy._origin("operator@bearer"), "operator-token")   # shared bearer
        self.assertEqual(sm_proxy._origin(""), "operator-token")
        self.assertEqual(sm_proxy._origin(None), "operator-token")

    def test_only_the_resolve_route_stamps_operator_click(self):
        # structural: no other engine/proxy path passes 'operator-click' into enqueue —
        # the default is code-shell, so a shell can never forge the operator.
        import inspect
        src = inspect.getsource(sm_proxy)
        # the sole producer is _origin, called only from the two authenticated routes
        self.assertEqual(src.count('"operator-click"'), 1)   # only inside _origin
        self.assertIn("initiated_by=origin", src)            # resolve threads the derived origin


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


class _FakeRecord(dict):
    """A record whose node is returned for ANY key access (['n'], ['r'], …)."""
    def __init__(self, node):
        super().__init__(node)
        self._node = node

    def __getitem__(self, k):
        return self._node


class _FakeResult(list):
    """A neo4j-Result-shaped list: iterable + .single()."""
    def single(self):
        return self[0] if self else None


class _FakeSession:
    """Canned read-only 7688 session: returns rows keyed by the label in the query."""
    _ROWS = {
        "SMKill": [{"id": "B-AG", "reason": "recently-decayed", "status": "killed"}],
        "SMWatch": [{"id": "B-AG", "detail": "recheck_due ~Dec 2026"}],
        "SMLesson": [{"text": "clustered inference: pooled name-day t inflates by ~sqrt(names)"}],
        "SMBoardItem": [{"id": "V-015"}],
        "SMRunRequest": [{"id": "run-1", "item_id": "V-015-TDF", "result": {"t": 1.09, "n": 160}}],
        "SMBuildNote": [{"id": "bn:data-completeness-floor", "dispatch": "data-completeness-floor",
                         "at": "2026-07-05T00:00:00Z", "built": "assert_pull_complete"}],
    }

    def run(self, cypher, **params):
        for label, rows in self._ROWS.items():
            if f":{label})" in cypher or f":{label} " in cypher:
                if "retained" in cypher:
                    return _FakeResult()
                return _FakeResult(_FakeRecord(dict(r)) for r in rows)
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDriver:
    def session(self, **kw):
        return _FakeSession()


class TestHandoffPack(unittest.TestCase):
    def test_handoff_composes_live_pack_readonly(self):
        old = sm_proxy._sm_driver
        sm_proxy._sm_driver = _FakeDriver()
        try:
            out = sm_proxy.sm_handoff()
        finally:
            sm_proxy._sm_driver = old
        md = out["markdown"]
        self.assertEqual(out["provenance"], "live-7688")
        for section in ("ROLES & LAWS", "HONEST CURRENT STATE", "RESEARCH STATE",
                        "ENGINEERING STATE", "HOW TO OPERATE"):
            self.assertIn(section, md)
        # a live pack CAN quote a banked lesson (the disk-fallback pack cannot)
        self.assertIn("clustered inference", md)
        # the non-negotiable firewall law is present (worded without the port literal so
        # the search-master pool holds no trading-instance reference — DEF-015)
        self.assertIn("research graph (7688) NEVER reaches the trading instance", md)
        self.assertGreater(out["words"], 500)


class TestDebrief(unittest.TestCase):
    def test_debrief_composes_four_voices_readonly(self):
        import sm_analyst
        old_d, old_raw = sm_proxy._sm_driver, sm_analyst.raw
        sm_proxy._sm_driver = _FakeDriver()
        # fake the proxy LLM: reporter/strategist plain, skeptic+prospector end with markers
        def fake_raw(system, user, max_tokens=700):
            if "SKEPTIC" in system:
                return {"text": "Suspicious.\n\n**SPARKS:**\n- Did we run a placebo?\n- Is n enough?"}
            if "PROSPECTOR" in system:
                return {"text": "Ore here.\n\n**GLINTS:**\n- Brick in the tail (t=1.09) — check: split it."}
            return {"text": "t=1.09 means weak evidence. NEXT CLICK: extend the window."}
        sm_analyst.raw = fake_raw
        try:
            out = sm_proxy.sm_debrief(sm_proxy.SMDebriefRequest(run_id="V-015-TDF"))
        finally:
            sm_proxy._sm_driver, sm_analyst.raw = old_d, old_raw
        for voice in ("reporter", "strategist", "skeptic", "prospector"):
            self.assertIn(voice, out)
        self.assertEqual(out["sparks"], ["Did we run a placebo?", "Is n enough?"])
        self.assertTrue(out["glints"] and "Brick in the tail" in out["glints"][0])
        self.assertIn("## DEBRIEF", out["markdown"])
        self.assertEqual(out["cost"]["passes"], 4)

    def test_debrief_unknown_run_is_honest(self):
        old = sm_proxy._sm_driver
        class _Empty(_FakeSession):
            def run(self, cypher, **p):
                return _FakeResult()                     # no run found
        class _D:
            def session(self, **k): return _Empty()
        sm_proxy._sm_driver = _D()
        try:
            out = sm_proxy.sm_debrief(sm_proxy.SMDebriefRequest(run_id="nope"))
        finally:
            sm_proxy._sm_driver = old
        self.assertIn("not found", out["error"])


class TestRulingsRender(unittest.TestCase):
    def test_refiled_kills_leave_the_kill_list_and_surface_separately(self):
        # a WATCH/OCCUPIED refile is no longer a kill-subtraction; it lands in `refiled`
        rm_kills = [{"id": "B-VAL", "status": "killed"}, {"id": "B-AG", "status": "watch"},
                    {"id": "B-GP", "status": "occupied"}]
        kills = [k for k in rm_kills if (k.get("status") or "killed") not in ("watch", "occupied")]
        refiled = [k for k in rm_kills if (k.get("status") or "") in ("watch", "occupied")]
        self.assertEqual([k["id"] for k in kills], ["B-VAL"])
        self.assertEqual({k["id"] for k in refiled}, {"B-AG", "B-GP"})


class TestUpdateAndChip(unittest.TestCase):
    def test_git_is_invoked_with_safe_directory_defeating_dubious_ownership(self):
        # DEF-016: runtime git under LocalSystem must pass safe.directory or it fails on
        # 'dubious ownership' — the chip then reads a stale stamp (the bug).
        self.assertIn("safe.directory=*", sm_proxy._SAFE)

    def test_git_update_is_loud_never_a_silent_noop(self):
        # a diverged tree returns ok=false WITH the exit code + ahead/behind + a reason;
        # never a bare success. (Runs against the real repo state — diverged or clean, the
        # contract holds: ok is a bool and a failure always carries a detail.)
        u = sm_proxy._git_update()
        self.assertIn("ok", u)
        self.assertIsInstance(u["ok"], bool)
        if not u["ok"]:
            self.assertTrue(u.get("detail"), "a failed update MUST report why (no silent no-op)")

    def test_read_commit_prefers_real_tree_head_over_a_stale_stamp(self):
        # the chip's truth is git HEAD (safe under LocalSystem now), stamp is only a fallback
        head = sm_proxy._git_short("HEAD")
        if head:
            self.assertEqual(sm_proxy._read_commit(), head)


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
        self.assertIn("/sm/probe/cancel", paths)         # operator abort of a running probe

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


class TestCommitStampNotRuntimeGit(unittest.TestCase):
    """The running commit is read from the deploy-time STAMP file, not a runtime git
    call — the LocalSystem service can't run git (dubious ownership), which is why the
    chip read 'unknown'. The stamp is the primary source."""
    def test_git_head_is_truth_stamp_is_the_git_less_fallback(self):
        # DEF-016 fix: the OLD model preferred the stamp over git — a stale stamp (hook
        # never fired) then made a current process read 'behind'. Corrected: the real tree
        # HEAD (git, safe under LocalSystem) is the truth; the stamp only fills in when git
        # is genuinely unavailable.
        import json, tempfile, os
        d = tempfile.mkdtemp()
        vf = os.path.join(d, "proxy_version.json")
        with open(vf, "w", encoding="utf-8") as f:
            json.dump({"commit": "deadbee", "branch": "main"}, f)     # a STALE stamp
        orig_vf, orig_git = sm_proxy._VERSION_FILE, sm_proxy._git_short
        sm_proxy._VERSION_FILE = vf
        try:
            sm_proxy._git_short = lambda ref="HEAD": "cafe123"        # git available → truth
            self.assertEqual(sm_proxy._read_commit(), "cafe123")     # git HEAD wins over stale stamp
            sm_proxy._git_short = lambda ref="HEAD": None             # git-less env → fallback
            self.assertEqual(sm_proxy._read_commit(), "deadbee")     # stamp fills in
            self.assertEqual(sm_proxy._stamped_commit(), "deadbee")
        finally:
            sm_proxy._VERSION_FILE, sm_proxy._git_short = orig_vf, orig_git

    def test_missing_stamp_reports_a_source_never_silent(self):
        import os
        orig = sm_proxy._VERSION_FILE
        sm_proxy._VERSION_FILE = os.path.join(os.path.dirname(orig), "does-not-exist.json")
        try:
            # no stamp → _stamped_commit None; commit_source is 'git' (dev) or 'unknown' —
            # never a silent lie. (The point: the source is always reported.)
            self.assertIsNone(sm_proxy._stamped_commit())
            self.assertIn(sm_proxy._commit_state()["commit_source"], ("git", "unknown"))
        finally:
            sm_proxy._VERSION_FILE = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
