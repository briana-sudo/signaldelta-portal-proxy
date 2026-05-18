"""
SignalDelta portal proxy — FastAPI service.

Exposes local Neo4j to the GitHub Pages portal over HTTPS via a TryCloudflare
quick tunnel. The portal POSTs {"name": "<query_name>", "params": {...}} with
Authorization: Bearer <PROXY_API_TOKEN>. The proxy looks up the Cypher string
from queries.QUERIES (whitelist), executes it against local Neo4j, and returns
the result as JSON with dates/datetimes serialized as ISO 8601 strings.

Architecture intent:
  - Portal CAN NOT inject arbitrary Cypher — only the 20 pre-authored names
    in queries.QUERIES are callable.
  - Engine remains untouched on local Neo4j at bolt://localhost:7687.
  - Auth is a single shared bearer token (PROXY_API_TOKEN). Operator rotates
    by re-generating and updating both ends.

Env vars (read from .env via python-dotenv):
  - PROXY_API_TOKEN   — required, 32+ chars
  - NEO4J_URI         — default bolt://localhost:7687
  - NEO4J_USER        — default neo4j
  - NEO4J_PASSWORD    — required
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from neo4j import GraphDatabase, time as neo4j_time
from pydantic import BaseModel, Field

from queries import QUERIES, REQUIRED_PARAMS

load_dotenv()

PROXY_API_TOKEN = os.environ.get("PROXY_API_TOKEN")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")

if not PROXY_API_TOKEN:
    raise RuntimeError(
        "PROXY_API_TOKEN env var missing. Copy .env.example to .env and set a value."
    )
if not NEO4J_PASSWORD:
    raise RuntimeError(
        "NEO4J_PASSWORD env var missing. Set it in your .env file."
    )

# Allow portal origin + localhost dev. Bearer auth is the actual security
# boundary; CORS just keeps browsers from blocking the preflight.
ALLOWED_ORIGINS = [
    "https://briana-sudo.github.io",
    "http://localhost:5173",
    "http://localhost:4173",
]


# ─── Neo4j driver singleton, opened/closed on app lifecycle ──────────────
_driver = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _driver
    _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        _driver.verify_connectivity()
    except Exception as e:
        print(f"[proxy] WARNING: Neo4j connectivity check failed: {e}")
    yield
    if _driver is not None:
        _driver.close()


app = FastAPI(
    title="SignalDelta Portal Proxy",
    description="Local Neo4j → HTTPS tunnel for GitHub Pages portal",
    version="0.1.0",
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
    """Reject requests without a valid bearer token."""
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
    """Recursively convert Neo4j temporal + driver types to JSON-safe primitives."""
    if value is None:
        return None
    # Neo4j-driver returns these types for date/datetime/time/duration.
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
        "version": "0.1.0",
        "queries_whitelisted": sorted(QUERIES.keys()),
    }


@app.get("/health")
def health():
    """Liveness check. Verifies Neo4j connectivity each call."""
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
    required = REQUIRED_PARAMS.get(req.name, [])
    missing = [k for k in required if k not in req.params]
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
            result = session.run(cypher, **req.params)
            rows = [coerce(dict(r)) for r in result]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Neo4j query failed: {e}")

    return QueryResponse(name=req.name, rows=rows, row_count=len(rows))
