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

from queries import CUTOFF_QUERIES, PORTAL_TRADE_CUTOFF_ISO, QUERIES, REQUIRED_PARAMS

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
        "endpoints": ["GET /", "GET /health", "POST /query (bearer)", "GET /macro_news (bearer)"],
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
