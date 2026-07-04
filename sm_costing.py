"""Costing worker (Part B) — behind the Data-needs "Price it / Research" button.

On a Price-it request for a gated surface, this RESEARCHES the real cost (vendor,
cost/yr, monthly option, terms, tiers, what-you-get) from a costing knowledge base
(public vendor pricing looked up during the build; sources noted per entry) and
returns filled fields. Where pricing is quote-only it returns "quote required".

It also surfaces the JUDGMENT CALLS the operator has to make (tier choice, discount
qualification, account setup) as `questions` — the assistant (Part C) works through
those in the analyst panel.

FIREWALL: the worker only RESEARCHES and fills fields. It never buys, never
onboards, never spends, never writes the graph. Buying stays behind the operator's
explicit Approve. (No onboard/resolve/purchase call exists in this module.)
"""
from __future__ import annotations

from typing import Any

QUOTE = "quote required"

# public vendor pricing, researched via web lookup (July 2026) — sources per entry.
# Keyed by surface keywords; first match wins.
_COSTING = [
    (("option", "skew", "implied", "vol"), {
        "vendor": "ORATS",
        "cost_yr": "$1,188-$3,588/yr (API) + $2,000 one-time historical",
        "monthly": "yes - $99-$299/mo (API tier)",
        "terms": "monthly API or annual; near-EOD history $2,000 one-time (since 2007)",
        "tiers": "DataShop / API / 1-min Intraday",
        "what_you_get": "IV surface, skew history, smoothed quotes + greeks (US equities/ETFs)",
        "source": "orats.com (researched 2026-07)",
        "questions": [
            {"kind": "tier", "q": "ORATS has DataShop / API / 1-min Intraday tiers. The vol-premium test needs the daily IV surface + skew history - the API tier (~$199/mo) fits. Do you also need 1-min intraday (adds $199/mo + $1,500 one-time)?"},
        ],
    }),
    (("crypto", "on-chain", "onchain", "glassnode"), {
        "vendor": "Glassnode",
        "cost_yr": "$948/yr (Pro) - ~$9,588/yr (Pro + API add-on)",
        "monthly": "yes - $79/mo (Pro); API add-on ~$799/mo",
        "terms": "monthly or annual; Institutional = custom, redistribution extra",
        "tiers": "Advanced / Professional / Institutional",
        "what_you_get": "on-chain metrics across 11 chains (SOPR, exchange balances, flows)",
        "source": "studio.glassnode.com/pricing (researched 2026-07)",
        "questions": [
            {"kind": "discount", "q": "Glassnode offers academic/startup discounts. Do you qualify (university affiliation or <2yr startup)? It can cut the Pro+API bundle materially."},
            {"kind": "tier", "q": "API access is only on the Professional tier (add-on), not Advanced. Confirm Professional + API (~$799/mo) vs Advanced charts-only ($49/mo)?"},
        ],
    }),
    (("funding", "amberdata", "perp", "basis"), {
        "vendor": "Amberdata",
        "cost_yr": QUOTE, "monthly": QUOTE,
        "terms": "institutional, annual license - quote only",
        "tiers": "Professional / Enterprise",
        "what_you_get": "perp funding + basis across venues",
        "source": "amberdata.io (quote-only, no public price 2026-07)",
        "questions": [
            {"kind": "setup", "q": "Amberdata is quote-only - it needs a sales contact + an account before pricing. Want me to scaffold the account-request (no credential held) so you can get a quote?"},
        ],
    }),
    (("futures", "managed-futures", "trend", "cta", "regime"), {
        "vendor": "Norgate Data",
        "cost_yr": "$270/yr (12-mo) / $148.50 (6-mo)",
        "monthly": "no - 6-mo or 12-mo only",
        "terms": "6 or 12 month; ~100 futures markets, continuous contracts",
        "tiers": "Futures package",
        "what_you_get": "continuous + spot futures, ~100 markets (incl. back-adjusted)",
        "source": "norgatedata.com/futurespackage.php (researched 2026-07)",
        "questions": [
            {"kind": "setup", "q": "Norgate futures also needs a broker/data-feed for LIVE execution of the managed-futures sleeve. The $270/yr covers RESEARCH data; live trading is a separate broker step. Proceed with research data first?"},
        ],
    }),
    (("relational", "graph", "linkage", "supply", "cross-firm"), {
        "vendor": "FactSet Revere / S&P Global (supply-chain)",
        "cost_yr": QUOTE, "monthly": QUOTE,
        "terms": "enterprise annual license - quote only",
        "tiers": "Enterprise",
        "what_you_get": "supplier/customer + ownership linkages (relational graph)",
        "source": "factset.com / spglobal.com (quote-only, institutional 2026-07)",
        "questions": [
            {"kind": "setup", "q": "Relational linkage data (FactSet Revere / S&P) is enterprise quote-only and pricey. A cheaper path: EDGAR-derived linkages (owned, free) may cover part of it. Research the vendor quote, or scope the free EDGAR alternative first?"},
        ],
    }),
    (("borrow", "short", "securities-lending", "utilization"), {
        "vendor": "Ortex (retail) / S3 Partners (institutional)",
        "cost_yr": "~$549-$2,400/yr (Ortex) / quote (S3)",
        "monthly": "yes - ~$49-$199/mo (Ortex)",
        "terms": "monthly or annual (Ortex); S3 = enterprise quote",
        "tiers": "Ortex Basic/Advanced/Pro; S3 enterprise",
        "what_you_get": "short interest, borrow rate, utilization, days-to-cover",
        "source": "ortex.com (researched 2026-07); s3partners.com (quote)",
        "questions": [
            {"kind": "tier", "q": "Ortex tiers differ by history depth + API. The borrow-gate test needs borrow rate + utilization history with API - the Advanced/Pro tier. Confirm API is required (vs the cheaper Basic web-only)?"},
        ],
    }),
]

_DEFAULT = {
    "vendor": QUOTE, "cost_yr": QUOTE, "monthly": QUOTE,
    "terms": QUOTE, "tiers": QUOTE, "what_you_get": QUOTE,
    "source": "no public pricing found - quote required",
    "questions": [{"kind": "setup", "q": "No public vendor pricing found for this surface - it needs a direct vendor quote. Want me to scaffold the vendor-contact request?"}],
}


def research(surface_id: str, surface: str = "") -> dict[str, Any]:
    """Fill the priceable fields for a gated surface (or 'quote required'), plus the
    judgment-call questions for the assistant. NEVER buys or onboards."""
    blob = f"{surface_id} {surface}".lower()
    entry = next((e for keys, e in _COSTING if any(k in blob for k in keys)), _DEFAULT)
    fields = {k: entry[k] for k in ("vendor", "cost_yr", "monthly", "terms", "tiers", "what_you_get")}
    return {
        "surface_id": surface_id,
        "queued": True,
        "researched": True,
        "fields": fields,
        "questions": entry.get("questions", []),
        "source": entry.get("source", ""),
        "note": "researched cost — no purchase, no onboarding. Approve is still yours.",
    }
