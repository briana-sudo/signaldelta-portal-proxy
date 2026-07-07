"""
SignalDelta portal proxy — FastAPI service.

Exposes local Neo4j to the GitHub Pages portal over HTTPS via a TryCloudflare
quick tunnel. The portal POSTs {"name": "<query_name>", "params": {...}} with
Authorization: Bearer <PROXY_API_TOKEN>. The proxy looks up the Cypher string
from queries.QUERIES (whitelist), executes it against local Neo4j, and returns
the result as JSON with dates/datetimes serialized as ISO 8601 strings.

Architecture intent:
  - Portal CAN NOT inject arbitrary Cypher — only the pre-authored names
    in queries.QUERIES are callable.
  - Engine remains untouched on local Neo4j at bolt://localhost:7687.
  - Auth is a single shared bearer token (PROXY_API_TOKEN).

Portal v1.1 additions (2026-05-26):
  - Cutoff filter (Change 1): main.run_query() auto-injects
    PORTAL_TRADE_CUTOFF_ISO as $cutoff into any query whose name is in
    queries.CUTOFF_QUERIES.
  - Macro news endpoint (Change 4): GET /macro_news pulls live from
    Alpha Vantage NEWS_SENTIMENT topic=economy_* feed, caches 60 min,
    serves stale on rate-limit up to 24 h.

Env vars (read from .env via python-dotenv):
  - PROXY_API_TOKEN       — required, 32+ chars
  - NEO4J_URI             — default bolt://localhost:7687
  - NEO4J_USER            — default neo4j
  - NEO4J_PASSWORD        — required
  - ALPHA_VANTAGE_API_KEY — required for /macro_news (engine shares the key)
"""
from __future__ import annotations

import json
import os
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, date, time, timezone
import hashlib
from threading import Lock, Thread
from zoneinfo import ZoneInfo
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from neo4j import GraphDatabase, time as neo4j_time
from pydantic import BaseModel, Field

from queries import (
    CORRUPT_EXCLUDE_QUERIES,
    CUTOFF_QUERIES,
    FORENSIC_QUERIES,
    PORTAL_TRADE_CUTOFF_ISO,
    QUERIES,
    REQUIRED_PARAMS,
    effective_corrupt_exclude_ids,
    effective_forensic_ids,
)

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))  # PROXY .env ONLY — never search up to the trading .env

PROXY_API_TOKEN = os.environ.get("PROXY_API_TOKEN")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")
# Accept three name variants (underscore / no-underscore / lowercase) so the
# proxy reads the key regardless of which convention the operator's .env uses.
# Engine's .env uses ALPHA_VANTAGE_API_KEY; the operator could plausibly type
# ALPHAVANTAGE_API_KEY (no underscore) — accept both.
ALPHA_VANTAGE_API_KEY = (
    os.environ.get("ALPHA_VANTAGE_API_KEY")
    or os.environ.get("ALPHAVANTAGE_API_KEY")
    or os.environ.get("alpha_vantage_api_key")
)

# Alpaca credentials for GET /market_calendar (market-status clock dispatch
# 2026-05-26). Same engine credentials — the engine uses Alpaca for paper
# execution. ALPACA_API_SECRET is the right name (NOT ALPACA_SECRET_KEY) per
# the May-2026 401-unauthorized incident memo. Accept both spellings just
# in case.
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
ALPACA_API_SECRET = (
    os.environ.get("ALPACA_API_SECRET")
    or os.environ.get("ALPACA_SECRET_KEY")
    or os.environ.get("APCA_API_SECRET_KEY")
)
# Calendar endpoint is the SAME on paper + live (calendar data is universal).
# Use paper-api to keep parity with the engine's Phase 1 paper account.
ALPACA_CALENDAR_BASE = "https://paper-api.alpaca.markets/v2/calendar"

# ── Item 10 PULL: secured WRITE path. A SEPARATE write token (not the read
# PROXY_API_TOKEN) so a read-token leak can never trigger a compute+write. The
# engine runs in its OWN venv via subprocess — the proxy stays a thin auth gate. ──
import subprocess
STORM_WRITE_TOKEN = os.environ.get("STORM_WRITE_TOKEN")
STORM_ENGINE_ROOT = os.environ.get("STORM_ENGINE_ROOT", r"C:\KCC_Local\storm-engine")
STORM_ENGINE_PY = os.path.join(STORM_ENGINE_ROOT, ".venv", "Scripts", "python.exe")
PULL_RADIUS_CAP_MI = 150.0       # HARD server-side cap (area ceiling: pi*150^2 ~ 70,686 mi^2)

if not PROXY_API_TOKEN:
    raise RuntimeError(
        "PROXY_API_TOKEN env var missing. Copy .env.example to .env and set a value."
    )
if not NEO4J_PASSWORD:
    raise RuntimeError("NEO4J_PASSWORD env var missing. Set it in your .env file.")
if not ALPHA_VANTAGE_API_KEY:
    print(
        "[proxy] WARNING: ALPHA_VANTAGE_API_KEY not set in .env (checked "
        "ALPHA_VANTAGE_API_KEY / ALPHAVANTAGE_API_KEY / alpha_vantage_api_key). "
        "/macro_news will return 503 until the key is added. Copy "
        ".env.example to .env and fill in the key, then restart the proxy.",
        file=sys.stderr,
    )
if not (ALPACA_API_KEY and ALPACA_API_SECRET):
    print(
        "[proxy] WARNING: ALPACA_API_KEY / ALPACA_API_SECRET not set in .env. "
        "/market_calendar will fall back to weekday-only (no holiday awareness). "
        "Set both and restart the proxy to enable the Alpaca calendar feed.",
        file=sys.stderr,
    )


ALLOWED_ORIGINS = [
    "https://briana-sudo.github.io",
    "http://localhost:5173",
    "http://localhost:4173",
]


# ─── Neo4j driver singleton ──────────────────────────────────────────────
_driver = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _driver
    _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        _driver.verify_connectivity()
    except Exception as e:
        print(f"[proxy] WARNING: Neo4j connectivity check failed: {e}", file=sys.stderr)
    # RESTART PROVENANCE (stamp-on-start): close the most-recent open ProxyRestartNode
    # with the commit we came back on, so every restart's loop is closed by the fresh
    # process (mirrors the engine's DEF-027). Best-effort — never blocks startup.
    try:
        from sm_proxy import stamp_return_on_start
        stamped = stamp_return_on_start()
        if stamped and stamped.get("id"):
            print(f"[proxy] restart provenance: closed {stamped['id']} "
                  f"(actor={stamped.get('actor')}, trigger={stamped.get('trigger')})", flush=True)
    except Exception as e:
        print(f"[proxy] restart provenance stamp-on-start skipped: {e}", file=sys.stderr)
    yield
    if _driver is not None:
        _driver.close()


app = FastAPI(
    title="SignalDelta Portal Proxy",
    description="Local Neo4j → HTTPS tunnel for GitHub Pages portal + macro news passthrough",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=86400,
)

# ─── Phase 3d-iii-a: search-master (7688) operator-surface route group ────────
# Additive + ISOLATED — a SEPARATE 7688 pool (never touches the 7687 `_driver`
# above), auth off the client (Cloudflare Access), read-only via the whitelist
# allowlist + read-mode sessions, resolve = the only write-mode session. See
# sm_proxy.py. The 7687 trading-engine endpoints below are untouched.
from sm_proxy import sm_router  # noqa: E402  (kept next to its mount for isolation)
app.include_router(sm_router)


# ─── Auth ─────────────────────────────────────────────────────────────────
def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):].strip()
    if token != PROXY_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


def require_write_bearer(authorization: str | None = Header(default=None)) -> None:
    """Separate, stronger gate for the WRITE endpoint (Item 10 PULL). Unauthenticated
    requests are rejected. Distinct from the read token so a read-token leak cannot write."""
    if not STORM_WRITE_TOKEN:
        raise HTTPException(status_code=503, detail="Write endpoint not configured (STORM_WRITE_TOKEN unset)")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if authorization[len("Bearer "):].strip() != STORM_WRITE_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


# ─── Request / response models ────────────────────────────────────────────
class QueryRequest(BaseModel):
    name: str = Field(..., description="Whitelisted query name from queries.QUERIES")
    params: dict[str, Any] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    name: str
    rows: list[dict[str, Any]]
    row_count: int


# ─── JSON-safe coercion (Neo4j temporal types → ISO 8601 strings) ────────
def coerce(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (neo4j_time.Date, neo4j_time.DateTime, neo4j_time.Time)):
        return value.iso_format()
    if isinstance(value, neo4j_time.Duration):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [coerce(v) for v in value]
    if isinstance(value, dict):
        return {k: coerce(v) for k, v in value.items()}
    return value


# ─── Endpoints ────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "signaldelta-portal-proxy",
        "version": "0.2.0",
        "queries_whitelisted": sorted(QUERIES.keys()),
        "endpoints": ["GET /", "GET /health", "POST /query (bearer)", "GET /macro_news (bearer)", "GET /market_calendar (bearer)", "GET /broker_account (bearer)", "GET /price_ticker (bearer)"],
        "portal_trade_cutoff_iso": PORTAL_TRADE_CUTOFF_ISO,
    }


@app.get("/health")
def health():
    try:
        if _driver is None:
            return {"status": "starting"}
        _driver.verify_connectivity()
        return {"status": "ok", "neo4j": "reachable"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Neo4j unreachable: {e}")


# ─── Scanner Tier 2 (2026-06-09): server-side GO enrichment ───────────────────
# The scanner_live_state query returns the engine's raw live gate inputs; the
# GO decision needs the ET clock + thresholds the engine doesn't expose, so it's
# computed here per poll. GO = G1 ∧ G2 ∧ G3 ∧ tradable-now ∧ fresh. Cash is
# deliberately NOT part of GO — stocks don't light when the equity market is
# closed (that's `tradable`).
_ET_TZ = ZoneInfo("America/New_York")
_AGGRESSIVE_BASE_THRESHOLD = 58    # §5 lowest track bar (clears ≥1 track). Canonical.
_SCANNER_FRESH_SECONDS = 12 * 60   # ≈2 bars; a stale row never GOes.


def _bucket_modifier_et(et_minutes: int) -> int:
    """§4.2 time-bucket modifier from ET minutes-since-midnight (covers 24h):
    High(+0) 9:30-11:30 & 14:00-16:00; Medium(+5) 7:00-9:30, 11:30-14:00,
    16:00-18:00, 20:00-22:00; Low(+10) 6:00-7:00, 18:00-20:00, 22:00-24:00;
    Dead(+15) 0:00-6:00."""
    m = et_minutes
    if (570 <= m < 690) or (840 <= m < 960):
        return 0   # High
    if (420 <= m < 570) or (690 <= m < 840) or (960 <= m < 1080) or (1200 <= m < 1320):
        return 5   # Medium
    if (360 <= m < 420) or (1080 <= m < 1200) or (1320 <= m < 1440):
        return 10  # Low
    return 15      # Dead 0:00-6:00


def _equity_market_open(now_et: datetime) -> bool:
    """Mon-Fri 9:30-16:00 ET (holidays not modeled, per dispatch spec)."""
    if now_et.weekday() >= 5:
        return False
    m = now_et.hour * 60 + now_et.minute
    return 570 <= m < 960


def _enrich_scanner_live_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Augment each ScannerLiveStateNode row with the GO decision + the five
    gate booleans, against the live ET clock. Read-only / pure. GO lights only
    when an asset is fireable RIGHT NOW: G1∧G2∧G3∧tradable∧fresh."""
    now_et = datetime.now(_ET_TZ)
    now_utc = datetime.now(timezone.utc)
    modifier = _bucket_modifier_et(now_et.hour * 60 + now_et.minute)
    market_open = _equity_market_open(now_et)
    out: list[dict[str, Any]] = []
    for r in rows:
        asset = r.get("asset")
        is_crypto = isinstance(asset, str) and "/" in asset
        comp = r.get("composite")
        composite = float(comp) if comp is not None else 0.0
        contributors = int(r.get("contributors") or 0)
        g1 = contributors >= 3
        g2 = bool(r.get("g2_agreed"))
        g3 = composite >= (_AGGRESSIVE_BASE_THRESHOLD + modifier)
        tradable = True if is_crypto else market_open
        fresh = False
        eval_ts = r.get("eval_ts")
        if eval_ts:
            try:
                dt = datetime.fromisoformat(str(eval_ts).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                fresh = (now_utc - dt).total_seconds() <= _SCANNER_FRESH_SECONDS
            except Exception:
                fresh = False
        out.append({
            **r,
            "composite": composite,
            "g1": g1, "g2": g2, "g3": g3,
            "tradable": tradable, "fresh": fresh,
            "go": g1 and g2 and g3 and tradable and fresh,
            "asset_class": "CRY" if is_crypto else "STK",
            "bucket_modifier": modifier,
        })
    return out


# ─── §6.6 annualization post-processors (2026-06-10) ─────────────────────────
# Operator-decided basis (do not re-derive): annualized = (1 + cum)^(252 /
# equity_days) − 1, where cum is the cohort's WINDOW cumulative return and
# equity_days = distinct EquitySnapshotNode days. insufficient_history =
# equity_days < 30, served ALONGSIDE the value (never suppressed). The
# annualization is computed HERE (proxy), never in the frontend.
def _compound_return(returns: list) -> float:
    """Cumulative return (fraction) of a cohort = ∏(1 + r/100) − 1 over the
    cohort's per-trade pnl_percent values."""
    acc = 1.0
    for r in returns or []:
        if r is None:
            continue
        try:
            acc *= 1.0 + float(r) / 100.0
        except (TypeError, ValueError):
            continue
    return acc - 1.0


def _annualize_pct(cum_fraction: float, equity_days: int) -> float | None:
    """(1 + cum)^(252 / equity_days) − 1, returned as a PERCENT. None when
    equity_days <= 0 or the cohort is ≤ −100% (annualized return undefined)."""
    if not equity_days or equity_days <= 0:
        return None
    base = 1.0 + cum_fraction
    if base <= 0.0:
        return None
    return (base ** (252.0 / equity_days) - 1.0) * 100.0


def _enrich_returns_by_domain_pct(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the annualized grid (cells + Total rim + Total corner) from the raw
    per-cell rows the Cypher returns. Each group is compounded over ITS trades
    then annualized — rims/corner are NOT sums of cell %s (annualized % is not
    additive), they recompound over the concatenated cohort, so the served
    population is identical to the $ view by construction."""
    if not rows:
        return []
    equity_days = int(rows[0].get("equity_days") or 0)
    insufficient = equity_days < 30
    groups: dict[str, dict[str, Any]] = {}

    def add(key, scope_type, track, ac, rets, pnl, n):
        g = groups.setdefault(key, {"scope_type": scope_type, "track": track,
                                    "asset_class": ac, "returns": [],
                                    "pnl_dollar": 0.0, "n": 0})
        g["returns"].extend(rets or [])
        g["pnl_dollar"] += float(pnl or 0.0)
        g["n"] += int(n or 0)

    for r in rows:
        tr = r.get("track")
        ac = r.get("asset_class")
        rets = r.get("returns") or []
        pnl = r.get("pnl_dollar") or 0.0
        n = r.get("n") or 0
        add(f"cell:{tr}:{ac}", "cell", tr, ac, rets, pnl, n)
        add(f"row:{tr}", "row_total", tr, None, rets, pnl, n)   # per track, all asset classes
        add(f"col:{ac}", "col_total", None, ac, rets, pnl, n)   # per asset class, all tracks
        add("corner", "corner", None, None, rets, pnl, n)

    out = []
    for g in groups.values():
        cum = _compound_return(g["returns"])
        ann = _annualize_pct(cum, equity_days)
        out.append({
            "scope_type": g["scope_type"],
            "track": g["track"],
            "asset_class": g["asset_class"],
            "n": g["n"],
            "pnl_dollar": round(g["pnl_dollar"], 2),
            "cum_return_pct": round(cum * 100.0, 4),
            "annualized_pct": (round(ann, 4) if ann is not None else None),
            "equity_days": equity_days,
            "insufficient_history": insufficient,
        })
    return out


def _enrich_pf_expectancy_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cumulative-to-date profit_factor + expectancy per exit-day for the KPI-tile
    sparklines. Cypher returns per-day gross_profit/gross_loss/sum_pnl/n (ordered);
    we accumulate so each point is PF/expectancy over all closes through that day."""
    if not rows:
        return []
    cum_gp = cum_gl = cum_pnl = 0.0
    cum_n = 0
    out = []
    for r in rows:
        cum_gp += float(r.get("gross_profit") or 0.0)
        cum_gl += float(r.get("gross_loss") or 0.0)
        cum_pnl += float(r.get("sum_pnl") or 0.0)
        cum_n += int(r.get("n") or 0)
        pf = (cum_gp / cum_gl) if cum_gl > 0 else None
        exp = (cum_pnl / cum_n) if cum_n > 0 else None
        out.append({
            "day": r.get("day"),
            "n": cum_n,
            "profit_factor": (round(pf, 4) if pf is not None else None),
            "expectancy_dollar": (round(exp, 4) if exp is not None else None),
        })
    return out


def _enrich_annualized_return(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Account-level annualized return for the header Ann field — same formula +
    flag. Cypher gives cum_return + equity_days; we annualize here."""
    if not rows:
        return []
    r = rows[0]
    equity_days = int(r.get("equity_days") or 0)
    cum = float(r.get("cum_return") or 0.0)
    ann = _annualize_pct(cum, equity_days)
    return [{
        "equity_days": equity_days,
        "latest_equity": r.get("latest_equity"),
        "capital_base": r.get("capital_base"),
        "cum_return_pct": round(cum * 100.0, 4),
        "annualized_pct": (round(ann, 4) if ann is not None else None),
        "insufficient_history": equity_days < 30,
    }]


@app.post("/query", response_model=QueryResponse, dependencies=[Depends(require_bearer)])
def run_query(req: QueryRequest):
    if req.name not in QUERIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown query name '{req.name}'. Valid: {sorted(QUERIES.keys())}",
        )

    # Portal v1.1 Change 1: auto-inject pre-market cutoff into params for
    # any whitelisted query that opted in via CUTOFF_QUERIES. Portal never
    # needs to know about the cutoff — it's a server-side policy.
    effective_params = dict(req.params)
    if req.name in CUTOFF_QUERIES:
        effective_params.setdefault("cutoff", PORTAL_TRADE_CUTOFF_ISO)
    # Session 40: auto-inject the forensic exclusion list for any query whose
    # Cypher references $forensic_ids. Portal never sends it — server-side policy.
    if req.name in FORENSIC_QUERIES:
        effective_params.setdefault("forensic_ids", effective_forensic_ids())
    # §6.6 (2026-06-10): auto-inject the frozen 36-id corrupt-close exclude list
    # for the all-time $-panels. Server-side policy; portal never sends it.
    if req.name in CORRUPT_EXCLUDE_QUERIES:
        effective_params.setdefault("corrupt_ids", effective_corrupt_exclude_ids())

    required = REQUIRED_PARAMS.get(req.name, [])
    missing = [k for k in required if k not in effective_params]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Query '{req.name}' requires params: {required}; missing: {missing}",
        )
    if _driver is None:
        raise HTTPException(status_code=503, detail="Driver not initialized")

    cypher = QUERIES[req.name]
    try:
        with _driver.session(database="neo4j", default_access_mode="READ") as session:
            result = session.run(cypher, **effective_params)
            rows = [coerce(dict(r)) for r in result]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Neo4j query failed: {e}")

    # Scanner Tier 2: compute the GO decision per asset server-side (ET clock +
    # thresholds the engine doesn't expose). Pure post-processing of the rows.
    if req.name == "scanner_live_state":
        rows = _enrich_scanner_live_state(rows)
    # §6.6 annualized post-processing (2026-06-10): the proxy computes the
    # annualization (operator formula) server-side — NEVER the frontend. The
    # Cypher returns raw inputs; these build the served annualized numbers.
    elif req.name == "panel_returns_by_domain_pct":
        rows = _enrich_returns_by_domain_pct(rows)
    elif req.name == "panel_annualized_return":
        rows = _enrich_annualized_return(rows)
    elif req.name == "panel_pf_expectancy_series":
        rows = _enrich_pf_expectancy_series(rows)

    return QueryResponse(name=req.name, rows=rows, row_count=len(rows))


# ─── Item 10: secured WRITE endpoint — on-demand PULL (compute + store) ──────
# NOT in the read-only /query whitelist. Token-gated (require_write_bearer, a
# SEPARATE write token). Validates auth + radius cap + CONUS + archive date, then
# shells out to the storm engine's OWN venv to run the canonical pipeline over the
# 150mi circle and persist chase data (in_geofence=False / 'chase', pull-namespaced).
class PullRequest(BaseModel):
    lat: float
    lon: float
    date: str
    radius_mi: float = Field(default=150.0)


PULL_SUBPROC_TIMEOUT = 600   # generous; compute is detached from the HTTP request


def _pull_center_hash(lat: float, lon: float) -> str:
    """Mirror storm.service.pull._center_hash EXACTLY so the job_id the proxy
    returns equals the one the engine writes on its PullJob marker."""
    return hashlib.md5(f"{float(lat):.3f},{float(lon):.3f}".encode()).hexdigest()[:8]


def _run_pull_detached(lat: float, lon: float, date: str, radius: float) -> None:
    """Run the engine pull subprocess to completion in a background thread. Its
    result is the PullJob marker it writes (run_pull) — the poll reads THAT, so we
    don't parse stdout here. Any failure is captured in the marker's 'error' state."""
    try:
        subprocess.run(
            [STORM_ENGINE_PY, "-m", "storm.service.pull",
             "--lat", str(lat), "--lon", str(lon),
             "--date", str(date), "--radius", str(radius)],
            cwd=STORM_ENGINE_ROOT, env={**os.environ, "PYTHONPATH": STORM_ENGINE_ROOT},
            capture_output=True, text=True, timeout=PULL_SUBPROC_TIMEOUT)
    except Exception:
        pass   # marker is source of truth; a hard crash leaves state='running' -> portal times out


@app.post("/storm_pull", dependencies=[Depends(require_write_bearer)])
def storm_pull(req: PullRequest):
    # HARD server-side guardrails (defense in depth; run_pull re-validates too).
    if req.radius_mi > PULL_RADIUS_CAP_MI + 1e-6:
        raise HTTPException(status_code=400,
                            detail=f"radius {req.radius_mi}mi exceeds the {PULL_RADIUS_CAP_MI}mi cap")
    if req.radius_mi <= 0:
        raise HTTPException(status_code=400, detail="radius must be positive")
    if not (20.0 <= req.lat <= 55.0 and -130.0 <= req.lon <= -60.0):
        raise HTTPException(status_code=400, detail="center is outside CONUS bounds")
    if str(req.date) < "2020-10-14":
        raise HTTPException(status_code=400, detail="date before operational archive start 2020-10-14")
    radius = min(req.radius_mi, PULL_RADIUS_CAP_MI)
    if not os.path.exists(STORM_ENGINE_PY):
        raise HTTPException(status_code=503, detail="storm engine venv not found on host")
    # The grib decode can run ~30s on this host (background-service CPU throttle) and
    # the first decode of a new date longer — well past the Netlify (10-26s) and tunnel
    # (~30s) ceilings. So this is FIRE-AND-FORGET: spawn the compute detached and return
    # a job_id immediately. The engine writes a PullJob lifecycle marker; the portal
    # polls the READ-ONLY storm_pull_status whitelist on that id. This handler is the
    # ONLY write trigger (write-bearer gated); the poll can never start a compute.
    job_id = f"pull-{req.date}-{_pull_center_hash(req.lat, req.lon)}"
    Thread(target=_run_pull_detached, args=(req.lat, req.lon, req.date, radius),
           daemon=True).start()
    return JSONResponse(status_code=202, content={
        "status": "accepted", "job_id": job_id, "date": req.date,
        "center": [req.lat, req.lon], "radius_mi": radius})


# ─── TEMPEST spend-dial + intraday-approve endpoints (2026-06-28) ────────────
# Same seam as /storm_pull: the proxy stays a thin auth gate and shells out to the storm
# engine's OWN venv CLI (storm.portal_api), which reuses spend_dial/notify. Read endpoints
# (-solve / approve-validate) use the read bearer; write endpoints (-approve / push / approve)
# use the SEPARATE write token. These are SYNCHRONOUS (solve/approve are seconds, unlike the
# 30s grib decode behind /storm_pull) — they print one JSON line we pass straight through.
SPEND_CLI_TIMEOUT = 150


def _run_engine_cli(command: str, payload: dict, timeout: int = SPEND_CLI_TIMEOUT) -> dict:
    """Invoke `python -m storm.portal_api <command> --json <payload>` in the engine venv and
    return the parsed last JSON line. The engine is the single source of the solve/approve."""
    if not os.path.exists(STORM_ENGINE_PY):
        raise HTTPException(status_code=503, detail="storm engine venv not found on host")
    try:
        proc = subprocess.run(
            [STORM_ENGINE_PY, "-m", "storm.portal_api", command, "--json", json.dumps(payload)],
            cwd=STORM_ENGINE_ROOT, env={**os.environ, "PYTHONPATH": STORM_ENGINE_ROOT},
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"engine {command} timed out")
    lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(status_code=500, detail=f"engine {command} no output: {(proc.stderr or '')[:300]}")
    try:
        return json.loads(lines[-1])
    except Exception:
        raise HTTPException(status_code=500, detail=f"engine {command} bad output: {lines[-1][:300]}")


class SpendSolveReq(BaseModel):
    date: str
    area_setting: str = "core_plus"
    spend_cap: float = 10000.0
    value_floor: float = 0.0
    peril: str = "hail"
    footprint_source: str = "canonical"
    phase: str = "SURGE"
    window: dict | None = None
    target_jobs: int | None = None
    remaining_annual_capacity: int | None = None


class SpendApproveReq(SpendSolveReq):
    bid_strategy: str = "target_impression_share"
    actor: str = "operator"
    campaign_id: str | None = None


class PushSubReq(BaseModel):
    subscription: dict
    operator: str = "brian"


class TokenReq(BaseModel):
    s: str
    footprint_source: str = "canonical"
    actor: str = "operator"


class SolveGeometryReq(BaseModel):
    date: str
    hail_floor: float = 1.80
    objective: str = "dial"
    peril: str = "hail"
    asof: str | None = None
    spend_cap: float | None = None
    persist: bool = False


@app.post("/spend_solve", dependencies=[Depends(require_bearer)])
def spend_solve(req: SpendSolveReq):
    return _run_engine_cli("spend-solve", req.dict())          # READ-ONLY (writes nothing)


@app.post("/solve_geometry", dependencies=[Depends(require_bearer)])
def solve_geometry(req: SolveGeometryReq):
    # SOLVE-GEOMETRY layer (fresh dial circles). READ-ONLY. The engine CLI tries the resident
    # KCCSolveEngine (~3s warm) and falls back to the cold path (~200s: two GPKG joins) — a longer
    # timeout than spend-solve so the cold first-solve of a date doesn't 504 before KCCSolveEngine
    # is installed. Warm solves return well under this.
    return _run_engine_cli("solve-geometry", req.dict(), timeout=240)


@app.post("/spend_approve", dependencies=[Depends(require_write_bearer)])
def spend_approve(req: SpendApproveReq):
    return _run_engine_cli("spend-approve", req.dict())        # the single gate (one record)


@app.post("/push", dependencies=[Depends(require_write_bearer)])
def push_subscribe(req: PushSubReq):
    return _run_engine_cli("push-subscribe", req.dict())       # off-graph subscription store


@app.post("/approve_validate", dependencies=[Depends(require_bearer)])
def approve_validate(req: TokenReq):
    return _run_engine_cli("approve-validate", req.dict())     # token -> CORE solve preview (read)


@app.post("/approve", dependencies=[Depends(require_write_bearer)])
def approve_token(req: TokenReq):
    return _run_engine_cli("approve-token", req.dict())        # deep-link approve (one record)


# ─── Portal v1.1 Change 4: GET /macro_news ────────────────────────────────
# Cache structure: single in-process dict, ms-precision wall-clock timestamps.
# All access guarded by MACRO_CACHE_LOCK. Cache TTL = 60 min (fresh window);
# stale serve up to 24 h when upstream rate-limits.
MACRO_CACHE: dict[str, Any] = {"data": None, "fetched_at_ms": 0, "status": "cold"}
MACRO_CACHE_LOCK = Lock()
MACRO_CACHE_TTL_MS = 60 * 60 * 1000        # 60 minutes
MACRO_CACHE_STALE_OK_MS = 24 * 60 * 60 * 1000  # 24 hours

AV_BASE_URL = "https://www.alphavantage.co/query"
AV_MACRO_PARAMS = {
    "function": "NEWS_SENTIMENT",
    "topics": "economy_monetary,economy_fiscal,economy_macro",
    "sort": "LATEST",
    "limit": "100",
}


def _build_av_macro_url() -> str:
    """Build the AV NEWS_SENTIMENT URL with macro topics + the engine's key."""
    params = {**AV_MACRO_PARAMS, "apikey": ALPHA_VANTAGE_API_KEY or ""}
    return f"{AV_BASE_URL}?{urllib.parse.urlencode(params)}"


def _fetch_av_macro() -> dict[str, Any]:
    """Blocking HTTP GET against Alpha Vantage. Returns parsed JSON dict."""
    url = _build_av_macro_url()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "signaldelta-portal-proxy/0.2"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def _is_rate_limit_response(data: dict[str, Any]) -> bool:
    """Alpha Vantage signals rate-limit by returning {"Information": "..."}
    in place of {"feed": [...]}. Detect that shape so we can fall back to
    cached data without surfacing the limit message to the portal."""
    if not isinstance(data, dict):
        return False
    return "Information" in data and "feed" not in data


@app.get("/macro_news", dependencies=[Depends(require_bearer)])
def macro_news():
    if not ALPHA_VANTAGE_API_KEY:
        # Per Portal v1.1 env-loader-fix dispatch: clear error body + stderr
        # log so the proxy's own log surfaces the actual reason. Portal sees
        # {error, feed: []} which keeps the MacroNewsStrip's empty path clean
        # while the operator still gets a banner indicating partial data.
        msg = "ALPHA_VANTAGE_API_KEY not configured in proxy .env"
        print(f"[proxy] /macro_news 503: {msg}", file=sys.stderr)
        return JSONResponse(
            status_code=503,
            content={"error": msg, "feed": []},
        )

    now_ms = int(_time.time() * 1000)
    with MACRO_CACHE_LOCK:
        cache_age_ms = now_ms - MACRO_CACHE["fetched_at_ms"]

        # Fresh cache window: return without hitting AV.
        if MACRO_CACHE["data"] is not None and cache_age_ms < MACRO_CACHE_TTL_MS:
            return {
                "feed": MACRO_CACHE["data"].get("feed", []),
                "cache": "fresh",
                "age_seconds": cache_age_ms // 1000,
                "fetched_at_ms": MACRO_CACHE["fetched_at_ms"],
            }

        # Cache expired (or cold) — try a live fetch.
        try:
            data = _fetch_av_macro()
        except Exception as e:
            print(f"[proxy] /macro_news fetch error: {e}", file=sys.stderr)
            # Network or upstream failure — serve stale if we have any.
            if (
                MACRO_CACHE["data"] is not None
                and cache_age_ms < MACRO_CACHE_STALE_OK_MS
            ):
                return {
                    "feed": MACRO_CACHE["data"].get("feed", []),
                    "cache": "stale",
                    "age_seconds": cache_age_ms // 1000,
                    "fetched_at_ms": MACRO_CACHE["fetched_at_ms"],
                    "warning": f"AV fetch failed; serving stale cache: {e}",
                }
            raise HTTPException(
                status_code=502,
                detail=f"Alpha Vantage fetch failed and no cache available: {e}",
            )

        if _is_rate_limit_response(data):
            msg = str(data.get("Information"))[:200]
            print(f"[proxy] /macro_news rate-limited: {msg}", file=sys.stderr)
            if (
                MACRO_CACHE["data"] is not None
                and cache_age_ms < MACRO_CACHE_STALE_OK_MS
            ):
                return {
                    "feed": MACRO_CACHE["data"].get("feed", []),
                    "cache": "stale",
                    "age_seconds": cache_age_ms // 1000,
                    "fetched_at_ms": MACRO_CACHE["fetched_at_ms"],
                    "warning": "AV rate-limited; serving stale cache",
                }
            return {
                "feed": [],
                "cache": "miss",
                "age_seconds": 0,
                "warning": "AV rate-limited; no cache available",
            }

        # Healthy response — cache and return.
        MACRO_CACHE["data"] = data
        MACRO_CACHE["fetched_at_ms"] = now_ms
        MACRO_CACHE["status"] = "fresh"
        return {
            "feed": data.get("feed", []),
            "cache": "fresh",
            "age_seconds": 0,
            "fetched_at_ms": now_ms,
        }


# ─── Market status clock dispatch (2026-05-26): GET /market_calendar ──────
# Returns the next 30 trading sessions from Alpaca's calendar endpoint, with
# a 24-hour proxy-side cache (the calendar is structurally static intraday —
# only changes when Alpaca publishes the next year's holiday list). Calendar
# rows include both regular trading days and the trimmed holiday-aware days
# (e.g., 1pm ET close on Black Friday). Portal computes OPEN / CLOSED /
# HOLIDAY state per second client-side from the cached calendar.
#
# Failure mode: if Alpaca rejects or times out, return {calendar: null,
# fallback: true}. The portal then falls back to "9:30-16:00 ET Mon-Fri,
# no holiday awareness" with a small "calendar unavailable" indicator.
MARKET_CAL_CACHE: dict[str, Any] = {
    "data": None,           # list of {date, open, close} dicts
    "fetched_at_ms": 0,
    "status": "cold",       # cold | fresh | stale | fallback
    "last_error": None,
}
MARKET_CAL_CACHE_LOCK = Lock()
MARKET_CAL_CACHE_TTL_MS = 24 * 60 * 60 * 1000   # 24 hours


def _fetch_alpaca_calendar() -> list[dict[str, Any]]:
    """Blocking GET against Alpaca calendar endpoint. Returns parsed list.

    Window: today UTC → today + 30 days. Alpaca's calendar response is a
    plain JSON array of {date, open, close, session_open, session_close}.
    Auth via APCA-API-KEY-ID + APCA-API-SECRET-KEY headers per Alpaca docs.
    """
    if not (ALPACA_API_KEY and ALPACA_API_SECRET):
        raise RuntimeError("Alpaca credentials not configured")
    today = datetime.utcnow().date()
    end = today.fromordinal(today.toordinal() + 30)
    params = {"start": today.isoformat(), "end": end.isoformat()}
    url = f"{ALPACA_CALENDAR_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
            "User-Agent": "signaldelta-portal-proxy/0.3",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    if not isinstance(parsed, list):
        raise RuntimeError(f"Alpaca calendar returned non-list: {type(parsed).__name__}")
    return parsed


@app.get("/market_calendar", dependencies=[Depends(require_bearer)])
def market_calendar():
    now_ms = int(_time.time() * 1000)
    with MARKET_CAL_CACHE_LOCK:
        cache_age_ms = now_ms - MARKET_CAL_CACHE["fetched_at_ms"]

        # Fresh cache window: return without hitting Alpaca.
        if (
            MARKET_CAL_CACHE["data"] is not None
            and cache_age_ms < MARKET_CAL_CACHE_TTL_MS
            and MARKET_CAL_CACHE["status"] == "fresh"
        ):
            return {
                "calendar": MARKET_CAL_CACHE["data"],
                "cache": "fresh",
                "age_seconds": cache_age_ms // 1000,
                "fetched_at_ms": MARKET_CAL_CACHE["fetched_at_ms"],
                "fallback": False,
            }

        # Cache expired/cold OR credentials missing — try a live fetch.
        try:
            data = _fetch_alpaca_calendar()
        except Exception as e:
            err_msg = str(e)
            print(f"[proxy] /market_calendar fetch error: {err_msg}", file=sys.stderr)
            # Serve stale if we have any usable cache.
            if (
                MARKET_CAL_CACHE["data"] is not None
                and cache_age_ms < MARKET_CAL_CACHE_TTL_MS * 7
            ):
                return {
                    "calendar": MARKET_CAL_CACHE["data"],
                    "cache": "stale",
                    "age_seconds": cache_age_ms // 1000,
                    "fetched_at_ms": MARKET_CAL_CACHE["fetched_at_ms"],
                    "fallback": False,
                    "warning": f"Alpaca fetch failed; serving stale cache: {err_msg}",
                }
            # No cache + can't fetch — portal must use weekday-only fallback.
            MARKET_CAL_CACHE["last_error"] = err_msg
            MARKET_CAL_CACHE["status"] = "fallback"
            return {
                "calendar": None,
                "cache": "miss",
                "age_seconds": 0,
                "fallback": True,
                "warning": err_msg,
            }

        # Healthy response — cache + return.
        MARKET_CAL_CACHE["data"] = data
        MARKET_CAL_CACHE["fetched_at_ms"] = now_ms
        MARKET_CAL_CACHE["status"] = "fresh"
        MARKET_CAL_CACHE["last_error"] = None
        return {
            "calendar": data,
            "cache": "fresh",
            "age_seconds": 0,
            "fetched_at_ms": now_ms,
            "fallback": False,
        }


# ─── Session 40 portal rebuild (2026-05-29): GET /broker_account ──────────
# Live Alpaca account + positions for the portal's live-state surfaces
# (Current Value, Open count, Today P&L numerator, trade-list current price,
# reconciliation indicator). NO caching — the portal polls every 60s and each
# poll triggers a fresh broker read, so the displayed equity is the broker's
# real-time number, not a stale graph-derived value (that was the entire point
# of the Session 40 sourcing decision).
#
# Uses the same APCA-API-KEY-ID / APCA-API-SECRET-KEY headers as
# /market_calendar against paper-api. On any Alpaca failure: 503 with
# {error, account:null, positions:[]} so the portal degrades gracefully
# (Account Bar falls back to dashes, no crash).
ALPACA_ACCOUNT_URL = "https://paper-api.alpaca.markets/v2/account"
ALPACA_POSITIONS_URL = "https://paper-api.alpaca.markets/v2/positions"

# ── Partial (b) swap cache (2026-06-01) ───────────────────────────────────
# 30s TTL on the two residual Alpaca /v2 calls used by /broker_account
# (account-level numerics now served from AccountStateNode in Neo4j; only
# last_equity + currency + status + positions list still need Alpaca). The
# cache absorbs poll bursts so we cannot re-trigger 429 even at high call
# rates. In-memory, per-process, cleared on restart.
_ALPACA_BROKER_CACHE_TTL_MS = 30_000
_alpaca_acct_cache: dict[str, Any] = {"value": None, "ts": 0}
_alpaca_positions_cache: dict[str, Any] = {"value": None, "ts": 0}


def _cached_alpaca_get(cache: dict[str, Any], url: str) -> Any:
    """30s TTL wrapper for Alpaca /v2 calls used by /broker_account. Stale-
    by-up-to-30s is acceptable: portal polls every 60s and the operator's
    Today P&L denominator (last_equity = broker prior-day close) doesn't
    move within a trading day."""
    now_ms = int(_time.time() * 1000)
    if cache["value"] is not None and (now_ms - cache["ts"]) < _ALPACA_BROKER_CACHE_TTL_MS:
        return cache["value"]
    value = _alpaca_get(url)
    cache["value"] = value
    cache["ts"] = now_ms
    return value


# Today P&L stale-basis guard (2026-06-06). AccountStateNode.portfolio_value
# (= equity + unrealized) is the basis the portal's Today P&L is measured against
# (equity − last_equity). If the M4 writer hasn't refreshed the node, that value
# can be stale-HIGH after an unrealized crypto spike reverts, inflating today-$
# (observed +$543.73). When the node is older than this many seconds, the handler
# falls back to LIVE Alpaca equity for the equity basis. Fresh nodes keep the
# graph value (preserving the partial-(b) 429 mitigation).
ACCOUNT_STATE_FRESH_S = 90


def _account_state_age_seconds(updated_at: Any) -> float | None:
    """Age in seconds of an AccountStateNode.updated_at value (ISO-8601 string or
    neo4j DateTime). Returns None when absent/unparseable — the caller treats
    that as STALE (can't confirm freshness → don't trust the graph basis)."""
    if not updated_at:
        return None
    try:
        if hasattr(updated_at, "to_native"):       # neo4j DateTime
            dt = updated_at.to_native()
        else:
            dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _load_account_state_for_broker() -> dict[str, Any] | None:
    """Read singleton AccountStateNode for the /broker_account ACCOUNT block.
    Returns None when no node exists yet (caller falls back to Alpaca for
    full backward-compat). Branch-isolated per project convention."""
    if _driver is None:
        return None
    cypher = (
        "MATCH (a:AccountStateNode) "
        "WHERE NOT a:KCCNode AND NOT a:KTMNode "
        "RETURN a.account_id AS account_id, "
        "       a.portfolio_value AS portfolio_value, "
        "       a.cash AS cash, "
        "       a.buying_power AS buying_power, "
        "       a.non_marginable_buying_power AS non_marginable_buying_power, "
        "       a.updated_at AS updated_at "
        "ORDER BY a.account_id ASC LIMIT 1"
    )
    try:
        with _driver.session(database="neo4j", default_access_mode="READ") as session:
            result = session.run(cypher)
            row = result.single()
            if not row:
                return None
            return {k: row[k] for k in row.keys()}
    except Exception as e:
        print(f"[proxy] /broker_account: AccountStateNode read failed ({e}); falling back to Alpaca", file=sys.stderr)
        return None


def _alpaca_get(url: str) -> Any:
    """Blocking authenticated GET against an Alpaca paper REST endpoint."""
    if not (ALPACA_API_KEY and ALPACA_API_SECRET):
        raise RuntimeError("Alpaca credentials not configured")
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
            "User-Agent": "signaldelta-portal-proxy/0.3",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


@app.get("/broker_account", dependencies=[Depends(require_bearer)])
def broker_account():
    """Portal v1.20 partial (b) swap (2026-06-01).

    ACCOUNT-level numeric fields now come from the engine-written
    AccountStateNode (Neo4j, 25-30s fresh) instead of Alpaca /v2/account
    on every poll. Removes the per-poll /v2/account hit when the M4
    writer is up — leaves a single CACHED /v2/account hit (30s TTL) to
    source `last_equity` (no node source) + `currency`/`status`. The
    /v2/positions call (per-symbol live current_price for the trade-row
    Current column + reconciliation pill) stays Alpaca-sourced because
    AccountStateNode has no positions[] array; that call is also
    30s-TTL cached. Net Alpaca call rate: ~4/minute regardless of
    portal poll frequency, vs the prior 2/poll = 2/minute baseline
    that was burst-prone under multi-client loads.

    Fallback: if AccountStateNode is absent (engine M4 not yet
    written), the handler falls back fully to Alpaca for backward
    compatibility — same response shape as the v1.6 implementation."""
    now_ms = int(_time.time() * 1000)

    node = _load_account_state_for_broker()

    alpaca_acct = None
    raw_positions = None
    alpaca_acct_err = None
    alpaca_pos_err = None
    try:
        alpaca_acct = _cached_alpaca_get(_alpaca_acct_cache, ALPACA_ACCOUNT_URL)
    except Exception as e:
        alpaca_acct_err = str(e)
        print(f"[proxy] /broker_account: Alpaca /v2/account fetch failed: {e}", file=sys.stderr)
    try:
        raw_positions = _cached_alpaca_get(_alpaca_positions_cache, ALPACA_POSITIONS_URL)
    except Exception as e:
        alpaca_pos_err = str(e)
        print(f"[proxy] /broker_account: Alpaca /v2/positions fetch failed: {e}", file=sys.stderr)

    # If we have NOTHING — no node + no Alpaca at all — keep the old 503
    # contract so the portal's PROXY ERROR banner still triggers.
    if node is None and alpaca_acct is None and raw_positions is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "alpaca_unavailable",
                "account": None,
                "positions": [],
                "fetched_at_ms": now_ms,
            },
        )

    def _f(d: Any, k: str) -> float | None:
        if not isinstance(d, dict):
            return None
        v = d.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # ── Account block: graph-first; Alpaca fallback if node missing. ──
    if node is not None:
        cash        = _f(node, "cash")
        bp          = _f(node, "buying_power")
        non_marg_bp = _f(node, "non_marginable_buying_power")
        account_id  = node.get("account_id")
        # Today P&L stale-basis guard (2026-06-06): the equity basis is graph
        # when the node is fresh, else LIVE Alpaca equity. cash/bp are not the
        # today-P&L basis and stay graph-sourced (kept the 429 mitigation).
        graph_equity  = _f(node, "portfolio_value")
        alpaca_equity = _f(alpaca_acct, "equity")
        age_s = _account_state_age_seconds(node.get("updated_at"))
        stale = (age_s is None) or (age_s > ACCOUNT_STATE_FRESH_S)
        if stale and alpaca_equity is not None:
            equity = alpaca_equity
            source = "graph_stale_equity_alpaca"   # node too old → live equity basis
        else:
            equity = graph_equity
            source = "graph" if not stale else "graph_stale_no_alpaca"  # stale but Alpaca down → best-effort graph
    else:
        equity      = _f(alpaca_acct, "equity")
        cash        = _f(alpaca_acct, "cash")
        bp          = _f(alpaca_acct, "buying_power")
        non_marg_bp = _f(alpaca_acct, "non_marginable_buying_power")
        account_id  = alpaca_acct.get("account_number") if isinstance(alpaca_acct, dict) else None
        source      = "alpaca"

    # last_equity, currency, status: Alpaca-only fields. Null when /v2/account
    # is unreachable; portal Today P&L falls back to EquitySnapshotNode.
    last_equity = _f(alpaca_acct, "last_equity")
    currency    = alpaca_acct.get("currency", "USD") if isinstance(alpaca_acct, dict) else "USD"
    status      = alpaca_acct.get("status") if isinstance(alpaca_acct, dict) else None

    account = {
        "equity": equity,
        # last_equity = broker's prior trading-day close. Session-40 v1.6
        # Today P&L denominator. Stays Alpaca-sourced — no node analog.
        "last_equity": last_equity,
        "cash": cash,
        "buying_power": bp,
        # New in v1.20: non_marg BP is exposed here too (was only on the M4
        # health strip). Portal can ignore; harmless additive field.
        "non_marginable_buying_power": non_marg_bp,
        "currency": currency,
        "account_number": account_id,
        "status": status,
        # Diagnostic — portal ignores; lets the operator confirm the partial
        # swap is hitting the graph path under steady state.
        "source": source,
    }

    positions = []
    if isinstance(raw_positions, list):
        for p in raw_positions:
            positions.append({
                "symbol": p.get("symbol"),
                "qty": _f(p, "qty"),
                "side": p.get("side"),
                "avg_entry_price": _f(p, "avg_entry_price"),
                "current_price": _f(p, "current_price"),
                "market_value": _f(p, "market_value"),
                "unrealized_pl": _f(p, "unrealized_pl"),
                "unrealized_plpc": _f(p, "unrealized_plpc"),
            })

    return {
        "account": account,
        "positions": positions,
        "fetched_at_ms": now_ms,
    }


# ─── Portal v1.11 (2026-05-29): GET /price_ticker ────────────────────────
# Live bottom-of-screen price ticker for the desktop portal. Replaces the
# hardcoded TICKER literal + cosmetic 0.1% wobble in placeholders.js.
#
# Symbol source: TradingConfigNode.monitored_assets (32-asset universe) —
# read via the same Neo4j driver the /query endpoint uses, no hardcoding.
# Split into stocks vs crypto by the '/' heuristic (BTC/USD is crypto,
# AAPL is stock) — matches Alpaca's symbol convention exactly.
#
# Data: two batched Alpaca snapshot calls per request — one for stocks
# (feed=sip explicit, account default could regress silently if billing
# lapses), one for crypto. Reuses the existing _alpaca_get helper. Up to
# ~50 symbols per batch per Alpaca docs; 32-universe well under. No cache
# — 60s portal poll already throttles outbound calls; paid SIP plan is
# 10000 req/min so 2/min is irrelevant.
#
# Per symbol normalize: price = latestTrade.p, prev_close = prevDailyBar.c
# (crypto: prior UTC-day close = standard "24h %" convention), change_pct
# rounded 2dp, direction 'u'/'d' for green/red, as_of_iso = latestTrade.t.
# Skip (with log) any symbol Alpaca doesn't snapshot — don't fail the whole
# call.
#
# Graceful degrade: 503 {error, stocks:[], crypto:[]} on Alpaca failure,
# mirroring /broker_account. Portal renders "PRICE FEED OFFLINE" rather
# than a frozen stale list.

ALPACA_STOCK_SNAPSHOTS_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
ALPACA_CRYPTO_SNAPSHOTS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/snapshots"


def _load_monitored_assets() -> list[str]:
    """Read monitored_assets from TradingConfigNode via the singleton driver."""
    if _driver is None:
        raise RuntimeError("Neo4j driver not initialized")
    cypher = QUERIES["monitored_assets"]
    with _driver.session(database="neo4j", default_access_mode="READ") as session:
        result = session.run(cypher)
        row = result.single()
        if not row:
            return []
        raw = row["asset_list"] or []
        return [str(s) for s in raw if s]


def _alpaca_snapshot(url: str, symbols: list[str]) -> dict[str, Any]:
    """Batched snapshot call. Returns {} on empty symbol list."""
    if not symbols:
        return {}
    qs = urllib.parse.urlencode({"symbols": ",".join(symbols)})
    if "stocks" in url:
        qs += "&feed=sip"
    full = f"{url}?{qs}"
    return _alpaca_get(full)


def _normalize_snapshot(symbol: str, snap: dict[str, Any], with_feed: bool) -> dict[str, Any] | None:
    """Normalize one Alpaca snapshot entry to the portal contract.
    Returns None when latestTrade.p or prevDailyBar.c is missing (skip)."""
    if not isinstance(snap, dict):
        return None
    latest = snap.get("latestTrade") or {}
    prev = snap.get("prevDailyBar") or {}
    price = latest.get("p")
    prev_close = prev.get("c")
    if price is None or prev_close is None or prev_close == 0:
        return None
    try:
        price_f = float(price)
        prev_f = float(prev_close)
    except (TypeError, ValueError):
        return None
    change_pct = round((price_f - prev_f) / prev_f * 100.0, 2)
    out = {
        "symbol": symbol,
        "price": price_f,
        "prev_close": prev_f,
        "change_pct": change_pct,
        "direction": "u" if change_pct >= 0 else "d",
        "as_of_iso": latest.get("t"),
    }
    if with_feed:
        out["feed"] = "sip"
    return out


@app.get("/price_ticker", dependencies=[Depends(require_bearer)])
def price_ticker():
    now_ms = int(_time.time() * 1000)

    # Symbol universe — straight from the graph, no hardcoding.
    try:
        symbols = _load_monitored_assets()
    except Exception as e:
        print(f"[proxy] /price_ticker monitored_assets read error: {e}", file=sys.stderr)
        return JSONResponse(
            status_code=503,
            content={"error": "monitored_assets_unavailable", "stocks": [], "crypto": [], "fetched_at_ms": now_ms},
        )

    # Split by Alpaca symbol convention: '/' → crypto (BASE/USD), else stock.
    stock_syms = sorted({s for s in symbols if "/" not in s})
    crypto_syms = sorted({s for s in symbols if "/" in s})

    # Two batched calls (don't loop per symbol).
    try:
        stock_raw = _alpaca_snapshot(ALPACA_STOCK_SNAPSHOTS_URL, stock_syms)
        crypto_raw_full = _alpaca_snapshot(ALPACA_CRYPTO_SNAPSHOTS_URL, crypto_syms)
    except Exception as e:
        print(f"[proxy] /price_ticker Alpaca fetch error: {e}", file=sys.stderr)
        return JSONResponse(
            status_code=503,
            content={"error": "alpaca_unavailable", "stocks": [], "crypto": [], "fetched_at_ms": now_ms},
        )

    # Crypto endpoint nests under {"snapshots": {...}}; stocks returns flat.
    crypto_raw = crypto_raw_full.get("snapshots", {}) if isinstance(crypto_raw_full, dict) else {}

    stocks_out: list[dict[str, Any]] = []
    crypto_out: list[dict[str, Any]] = []
    skipped: list[str] = []

    for sym in stock_syms:
        row = _normalize_snapshot(sym, stock_raw.get(sym), with_feed=True)
        if row is None:
            skipped.append(sym)
        else:
            stocks_out.append(row)
    for sym in crypto_syms:
        row = _normalize_snapshot(sym, crypto_raw.get(sym), with_feed=False)
        if row is None:
            skipped.append(sym)
        else:
            crypto_out.append(row)

    if skipped:
        print(f"[proxy] /price_ticker skipped (no snapshot): {','.join(skipped)}", file=sys.stderr)

    return {
        "fetched_at_ms": now_ms,
        "stocks": stocks_out,
        "crypto": crypto_out,
    }
