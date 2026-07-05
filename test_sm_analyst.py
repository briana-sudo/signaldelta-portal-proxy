"""Analyst firewall — the ask path is read-only by construction, and the fallback is
never an empty shell. Stdlib unittest."""
import inspect
import unittest

import sm_analyst


class AnalystFirewallTest(unittest.TestCase):
    def test_no_write_capability_in_the_ask_path(self):
        import ast
        src = inspect.getsource(sm_analyst)
        # (1) imports NO write module
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported |= {n.name.split(".")[0] for n in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for bad in ("run_queue", "sm_lessons", "sm_secrets", "neo4j", "sm_proxy_control", "sm_engine"):
            self.assertNotIn(bad, imported, f"analyst must not import {bad}")
        # (2) makes NO write CALL (paren form, so prose mentions don't false-positive)
        for bad in (".enqueue(", ".resolve(", ".onboard(", ".bank(", "GraphDatabase(", "_secrets."):
            self.assertNotIn(bad, src, f"analyst must not call {bad}")
        # (3) the only outbound target is the Anthropic API
        self.assertIn("api.anthropic.com", src)
        # (4) NO trading-.env crossing: no _ENV_FILES, no read of the trading root .env
        self.assertNotIn("_ENV_FILES", src)
        self.assertNotIn('_ROOT / ".env"', src)  # no trading root .env path


    def test_errored_run_surfaces_error_message_in_pack(self):
        # an errored run must reach the pack as an error_message (verbatim), not a gate.
        state = {"runs": [{"recipe_id": "V-015-TDF-FULL", "disposition": "error",
                           "error": "FeedUnavailable: point-in-time universe resolved to 0 names"}]}
        pack = sm_analyst.assemble_pack(state)
        self.assertIn("error_message", pack)
        self.assertIn("FeedUnavailable: point-in-time universe resolved to 0 names", pack)

    def test_system_prompt_scopes_7688_and_bars_7687(self):
        # architecture hard-rule: discovery=7688 is the analyst's world; 7687/trading is
        # OUT OF SCOPE and never a place it directs the operator.
        sys = sm_analyst._SYSTEM
        self.assertIn("7688", sys)
        self.assertIn("7687", sys)                          # named only to forbid it
        low = sys.lower()
        self.assertIn("out of scope", low)
        self.assertIn("never", low)

    def test_fallback_is_honest_not_empty(self):
        orig = sm_analyst._anthropic_key
        sm_analyst._anthropic_key = lambda: None            # simulate no key
        try:
            r = sm_analyst.answer("anything", [], {})
            self.assertFalse(r["grounded"])
            self.assertTrue(r["explanation"].strip())        # NEVER an empty shell
            self.assertIn("can't answer", r["explanation"].lower())
        finally:
            sm_analyst._anthropic_key = orig


if __name__ == "__main__":
    unittest.main()
