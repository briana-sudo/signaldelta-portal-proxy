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
from datetime import datetime, date, time
from threading import Lock
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from neo4j import GraphDatabase, time as neo4j_time
from pydantic import BaseModel, Field

from queries import (
    CUTOFF_QUERIES,
    FORENSIC_QUERIES,
    PORTAL_TRADE_CUTOFF_ISO,
    QUERIES,
    REQUIRED_PARAMS,
    effective_forensic_ids,
)

load_dotenv()

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


# ─── Auth ─────────────────────────────────────────────────────────────────
def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):].strip()
    if token != PROXY_API_TOKEN:
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
        "endpoints": ["GET /", "GET /health", "POST /query (bearer)", "GET /macro_news (bearer)", "GET /market_calendar (bearer)", "GET /broker_account (bearer)"],
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

    return QueryResponse(name=req.name, rows=rows, row_count=len(rows))


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
    now_ms = int(_time.time() * 1000)
    try:
        acct = _alpaca_get(ALPACA_ACCOUNT_URL)
        raw_positions = _alpaca_get(ALPACA_POSITIONS_URL)
    except Exception as e:
        print(f"[proxy] /broker_account fetch error: {e}", file=sys.stderr)
        return JSONResponse(
            status_code=503,
            content={"error": "alpaca_unavailable", "account": None, "positions": []},
        )

    def _f(d: dict[str, Any], k: str) -> float | None:
        v = d.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    account = {
        "equity": _f(acct, "equity"),
        "cash": _f(acct, "cash"),
        "buying_power": _f(acct, "buying_power"),
        "currency": acct.get("currency", "USD"),
        "account_number": acct.get("account_number"),
        "status": acct.get("status"),
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
