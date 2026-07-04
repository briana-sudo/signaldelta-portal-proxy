"""Costing worker (Part B) — fills real priceable fields, never buys/onboards.
Stdlib unittest (runnable with `python -m unittest test_sm_costing`)."""
import inspect
import unittest

import sm_costing


class CostingTest(unittest.TestCase):
    def test_fills_real_fields_for_a_priced_surface(self):
        r = sm_costing.research("options_skew", "Options · skew")
        self.assertTrue(r["researched"])
        f = r["fields"]
        self.assertEqual(f["vendor"], "ORATS")
        self.assertIn("$", f["cost_yr"])                 # a real number, not blank
        for key in ("vendor", "cost_yr", "monthly", "terms", "tiers", "what_you_get"):
            self.assertIn(key, f)
        self.assertTrue(r["source"])                     # sourced

    def test_quote_only_surface_marks_quote_required(self):
        r = sm_costing.research("relational_graph", "Relational · graph")
        self.assertEqual(r["fields"]["cost_yr"], "quote required")

    def test_surfaces_judgment_questions(self):
        r = sm_costing.research("crypto_onchain", "Crypto · on-chain")
        kinds = {q["kind"] for q in r["questions"]}
        self.assertTrue(kinds & {"tier", "discount", "setup"})   # a real judgment call

    def test_unknown_surface_defaults_to_quote_required(self):
        r = sm_costing.research("totally_unknown_surface", "mystery")
        self.assertEqual(r["fields"]["vendor"], "quote required")

    def test_firewall_no_buy_onboard_or_spend_CALL(self):
        # strip comments/docstrings — the prose legitimately says "never onboards"
        code = "".join(l for l in inspect.getsource(sm_costing).splitlines(True)
                       if not l.lstrip().startswith(("#", '"', "'")))
        for bad in (".onboard(", ".resolve(", ".purchase(", ".buy(", "_secrets", "GraphDatabase", ".set("):
            self.assertNotIn(bad, code)
        # and it imports no write/secret surface
        for bad in ("import neo4j", "from sm_secrets", "import sm_secrets"):
            self.assertNotIn(bad, inspect.getsource(sm_costing))


if __name__ == "__main__":
    unittest.main()
