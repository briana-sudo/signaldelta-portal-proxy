"""Phase 3 slice 3d-iii-a — the SEARCH-MASTER (7688) PROXY ROUTE GROUP.

The transport for the operator surface's search-master side: query / resolve /
onboarding / export / analyst, exposed over the Portal's FastAPI proxy. The proxy
WIRES, AUTHENTICATES, and ENFORCES read-only + isolation; the engine LOGIC is the
3d-i / 3d-ii package (a config swap binds it once 7688 is live).

Firewall built here (spec §1.1 Rev 3.3 · §6):
  * AUTH off the client — every endpoint (read AND resolve) requires the
    authenticated operator identity the Cloudflare-Access tunnel injects
    (``Cf-Access-Authenticated-User-Email``); absent → rejected. The browser
    bundle cannot forge it (auth is at the tunnel, not the client).
  * READ-ONLY, three layers — (1) read-mode sessions (``default_access_mode=READ``)
    on every read; (2) the WHITELIST Cypher allowlist (``sm_cypher_allowlist``);
    (3) the resolve path is the ONLY write-mode session, server-side.
  * ISOLATION (§6) — a SEPARATE 7688 connection pool that reaches ONLY 7688. This
    module holds no trading-engine driver and never references that instance, so it
    is structurally incapable of constructing a path to it. Instance-separation is
    the boundary; the KCC/KTM label filter on 7688 reads is a within-instance
    hygiene backstop, not the isolation.
  * SECRETS server-side — onboarding credentials go to ``SecretsStore`` (write-
    only, configured-only); never returned, logged, or echoed.

NOTE (no live infra): 7688 is provisioned by the operator (prepared script); until
then the read/resolve driver calls raise 503, but the auth + allowlist + isolation
gates run FIRST and are fully testable without a live DB or any secret.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from sm_cypher_allowlist import is_read_shaped
from sm_engine import engine_start, engine_status, engine_stop
from sm_proxy_control import helper_available, proxy_restart, proxy_start, proxy_status, proxy_stop
from sm_secrets import SecretsStore

# --- 7688 isolation: the search-master pool reaches ONLY 7688 ----------------
# A SEPARATE driver from the proxy's trading-engine ``_driver`` (main.py). This
# module never imports or references that driver — the search-master path holds
# only a 7688 connection (spec §6). Default is bolt://localhost:7688; the
# operator's 7688 provisioning script starts that instance.
SM_NEO4J_URI = os.environ.get("SM_NEO4J_URI", "bolt://localhost:7688")
SM_NEO4J_USER = os.environ.get("SM_NEO4J_USER", "neo4j")
SM_NEO4J_PASSWORD = os.environ.get("SM_NEO4J_PASSWORD")
SM_NEO4J_DATABASE = os.environ.get("SM_NEO4J_DATABASE", "neo4j")

# the KCC/KTM within-instance hygiene backstop (NOT the isolation — §6)
_BRANCH_ISOLATION = "NOT n:KCCNode AND NOT n:KTMNode"

_sm_driver = None                                # lazily-opened 7688 driver singleton
_secrets = SecretsStore(os.environ.get("SM_SECRETS_DIR"))
_rejection_sink: list[dict[str, Any]] = []       # allowlist rejections → context-monitor


def get_sm_driver():
    """Open (once) the 7688 driver. Separate pool from the trading engine. Raises
    503 until the operator has provisioned 7688 (prepared script) + set SM_NEO4J_PASSWORD."""
    global _sm_driver
    if _sm_driver is not None:
        return _sm_driver
    if not SM_NEO4J_PASSWORD:
        raise HTTPException(status_code=503,
                            detail="search-master 7688 not configured (SM_NEO4J_PASSWORD unset — run the 7688 provisioning step)")
    from neo4j import GraphDatabase       # imported lazily so the module loads without a live DB
    # 7688 is a pure SM-only instance; the KCC/KTM hygiene filter matches nothing
    # there, so silence the "label does not exist" notifications (harmless noise).
    try:
        _sm_driver = GraphDatabase.driver(SM_NEO4J_URI, auth=(SM_NEO4J_USER, SM_NEO4J_PASSWORD),
                                          notifications_min_severity="OFF")
    except TypeError:                     # older driver without the kwarg
        _sm_driver = GraphDatabase.driver(SM_NEO4J_URI, auth=(SM_NEO4J_USER, SM_NEO4J_PASSWORD))
    return _sm_driver


def _log_allowlist_rejection(source: str, cypher: str, reason: str) -> None:
    """A rejected read-path write is a DRIFT SIGNAL (§1.1 Rev 3.3(b)) — routed to
    the context-monitor (trading-engine-write / gating detector). Here it is
    recorded to a sink the engine's context-monitor drains; the VALUE is never a
    secret (it is a rejected query shape)."""
    _rejection_sink.append({"source": source, "reason": reason,
                            "query_prefix": (cypher or "")[:120], "drift": "read-path-write-attempt"})
    print(f"[sm-proxy] ALLOWLIST REJECT ({source}): {reason}", flush=True)


def rejection_log() -> list[dict[str, Any]]:
    return list(_rejection_sink)


# --- AUTH: the authenticated operator identity must be present (§1.1) ---------
def require_operator_identity(
        cf_authenticated_user_email: str | None = Header(default=None, alias="Cf-Access-Authenticated-User-Email"),
        authorization: str | None = Header(default=None),
) -> str:
    """Auth: PREFER Cloudflare-Access (auth off the client — the tunnel injects the
    operator email, §1.1). Where Access isn't configured on the tunnel, fall back to
    the same operator bearer token the trading portal already uses (PROXY_API_TOKEN)
    — so the operator has an authenticated identity either way. Absent/invalid both →
    reject. Applies to EVERY /sm/* endpoint, read and resolve."""
    if isinstance(cf_authenticated_user_email, str) and cf_authenticated_user_email:
        return cf_authenticated_user_email
    token = os.environ.get("PROXY_API_TOKEN")
    if isinstance(authorization, str) and authorization.startswith("Bearer ") and token \
            and authorization[len("Bearer "):].strip() == token:
        return "operator@bearer"
    raise HTTPException(status_code=401,
                        detail="unauthenticated: no operator identity (Cloudflare Access header or valid bearer token required)")


# --- request models ----------------------------------------------------------
class SMQueryRequest(BaseModel):
    cypher: str = Field(..., description="a read-shaped Cypher query (allowlist-gated)")
    params: dict[str, Any] = Field(default_factory=dict, description="parameters (NL→query emits parameterized Cypher)")


class SMResolveRequest(BaseModel):
    gate_item_id: str
    decision: Any                                 # approve | reject | {choose: option_id}
    gate_item_version: int


class SMOnboardRequest(BaseModel):
    source_id: str
    entitlement: str
    credential: str | None = Field(default=None, description="operator-supplied at runtime; stored server-side, never returned")
    watermark: str = ""
    content_hash: str = ""


class SMResearchRequest(BaseModel):
    surface_id: str
    surface: str = ""
    kind: str = "price-research"


class SMAnalystRequest(BaseModel):
    ask: str
    history: list[dict[str, Any]] = Field(default_factory=list)


class SMLessonProposeRequest(BaseModel):
    text: str
    source: str = ""
    lesson_id: str | None = None


class SMLessonActionRequest(BaseModel):
    lesson_id: str


# --- the router --------------------------------------------------------------
sm_router = APIRouter(prefix="/sm", tags=["search-master"])


def _read_query(cypher: str, params: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    """Run a read-shaped query on the 7688 read path: allowlist FIRST, then a
    READ-mode session. The allowlist + auth gates run before any driver call, so a
    write attempt is rejected before it can reach the DB (and without needing a
    live DB)."""
    verdict = is_read_shaped(cypher)
    if not verdict.allowed:
        _log_allowlist_rejection(source, cypher, verdict.reason)
        raise HTTPException(status_code=400, detail=f"query rejected by read allowlist: {verdict.reason}")
    driver = get_sm_driver()                      # 503 until 7688 provisioned
    with driver.session(database=SM_NEO4J_DATABASE, default_access_mode="READ") as session:
        result = session.run(cypher, **params)    # values are PARAMETERS, never concatenated
        return [dict(r) for r in result]


@sm_router.post("/query", dependencies=[Depends(require_operator_identity)])
def sm_query(req: SMQueryRequest):
    """REST read interrogation over the 7688 read model (§5). Allowlist-gated,
    READ-mode session, 7688-only."""
    rows = _read_query(req.cypher, req.params, source="rest")
    return {"rows": rows, "row_count": len(rows)}


@sm_router.post("/export", dependencies=[Depends(require_operator_identity)])
def sm_export(req: SMQueryRequest):
    """MD state-export input (§8.2) — read-only slice fetch (same allowlist + READ
    session). The markdown rendering is 3d-i's ``export_md``; this is the read that
    feeds it. No secrets (the read model carries none)."""
    rows = _read_query(req.cypher, req.params, source="export")
    return {"rows": rows, "row_count": len(rows)}


@sm_router.get("/readmodel", dependencies=[Depends(require_operator_identity)])
def sm_readmodel():
    """Reconstruct the operator-surface read model (the 7 slices) from the LIVE 7688
    graph — the board/map/ledger/kills/gated the portal renders, read straight from
    the DB (read-mode, branch-isolated). JSON-flattened fields (cells/meta/options)
    are un-flattened. This is the live-state read the portal uses."""
    driver = get_sm_driver()

    def unflat(node) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in dict(node).items():
            if isinstance(v, str) and v[:1] in "[{":
                try:
                    out[k] = json.loads(v)
                except Exception:
                    out[k] = v
            else:
                out[k] = v
        return out

    with driver.session(database=SM_NEO4J_DATABASE, default_access_mode="READ") as session:
        def rows(label: str, order: str = "") -> list[dict[str, Any]]:
            cy = f"MATCH (n:{label}) WHERE {_BRANCH_ISOLATION} RETURN n{order}"
            return [unflat(r["n"]) for r in session.run(cy)]
        state = rows("SMState")
        return {
            "board": rows("SMBoardItem"),
            "state": state[0] if state else {},
            "grid": rows("SMGridCell"),
            "ledger": rows("SMComponent"),
            "kills": rows("SMKill"),
            "gated": rows("SMGatedSurface"),
            "deployed": rows("SMDeployedSignal"),
            # revival monitor state + recheck-scan history (the Timeline's live source)
            "watches": rows("SMWatch"),
            "scan_history": rows("SMScan", order=" ORDER BY n.at DESC"),
        }


import sys as _sys


def _sm_engine():
    """Import the search-master engine (queue + recipe registry) once."""
    if r"C:\SignalDelta_Local" not in _sys.path:
        _sys.path.insert(0, r"C:\SignalDelta_Local")
    from searchmaster.engine import recipe_registry, run_queue
    return run_queue, recipe_registry


@sm_router.post("/resolve", dependencies=[Depends(require_operator_identity)])
def sm_resolve(req: SMResolveRequest):
    """Approve/Hold on a board item. Approve on a runnable candidate ENQUEUES ALL of
    its COMPONENT recipe runs (e.g. V-015 -> its 3 flows), one-at-a-time; the item
    disposes fully only when every component concludes. Hold parks it; other tiers
    route to their gated flow (surfaced, not run).

    FIREWALL: this only writes run-REQUESTS or a hold — it never runs the probe,
    buys, or onboards. The RUNNER (in SignalDeltaDiscovery) does the research."""
    decision = str(req.decision).lower()
    if decision in ("reject", "hold"):
        return {"resolved": True, "new_status": "HELD", "decision": "hold", "held": True}

    try:
        run_queue, registry = _sm_engine()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"engine unavailable: {e}")

    comps = registry.components_for(req.gate_item_id)
    if comps:
        try:
            states = [run_queue.enqueue(req.gate_item_id, rid,
                                        title=registry.get_recipe(rid)["name"]).get("state")
                      for rid in comps]
        except Exception as e:                        # 7688 down → honest 503
            raise HTTPException(status_code=503, detail=f"queue write failed: {e}")
        return {"resolved": True, "new_status": "QUEUED", "decision": "approve",
                "enqueued": True, "components": comps, "states": states}
    # non-runnable / no recipe → routed to the operator gate (surfaced, not run)
    return {"resolved": True, "new_status": "AT-GATE", "decision": "approve",
            "enqueued": False, "routed": "operator-gate",
            "note": "no runnable recipe for this item — routed to its gated flow (needs data/build/broker)."}


@sm_router.get("/probe/status", dependencies=[Depends(require_operator_identity)])
def sm_probe_status():
    """Live run state for the In-progress tab: the currently-running probe (with its
    stage-by-stage progress), the queue in order, and recent finished runs — read
    from 7688 so a browser refresh survives."""
    try:
        run_queue, _ = _sm_engine()
        return run_queue.status()
    except Exception:
        return {"running": None, "queue": [], "done": []}


@sm_router.post("/onboard", dependencies=[Depends(require_operator_identity)])
def sm_onboard(req: SMOnboardRequest):
    """Onboarding write path (§9). A credential (operator-supplied at runtime) goes
    to the SERVER-SIDE secrets store and is NEVER returned, logged, or echoed. The
    response references only "configured" + watermark/version — never the key. The
    actual validate→register→3b-un-gate runs in the engine (3d-i onboarding)."""
    configured = False
    if req.credential is not None:
        _secrets.set(f"{req.source_id}_api_key", req.credential)     # server-side only
        configured = True
    # source record — NEVER the credential value
    return {"source_id": req.source_id, "entitlement": req.entitlement,
            "configured": configured or _secrets.configured(f"{req.source_id}_api_key"),
            "watermark": req.watermark, "version_hash": req.content_hash,
            "note": "credential stored server-side (out-of-band); engine validate/register runs on the 7688 config swap"}


# --- COSTING WORKER (Part B) — "Price it / Research" fills the Data-needs fields ---
@sm_router.post("/research", dependencies=[Depends(require_operator_identity)])
def sm_research(req: SMResearchRequest):
    """Research the real cost of a gated surface (vendor, cost/yr, monthly, terms,
    tiers, what-you-get) and return filled fields + the operator's judgment-call
    questions. FIREWALL: researches + fills only — NEVER buys, onboards, or spends;
    Approve stays the operator's. (No onboard/resolve/secrets call in this path.)"""
    import sm_costing
    return sm_costing.research(req.surface_id, req.surface)


# --- ANALYST (real LLM, grounded) + GATED LEARNING (SMLesson) -----------------
def _analyst_state() -> dict[str, Any]:
    """Assemble the read-only grounding state: live board/watches/scans (7688) +
    recent run results (with date-range/universe) + banked lessons. Reads only."""
    state: dict[str, Any] = {}
    try:
        rm = sm_readmodel()
        state.update({"board": rm.get("board"), "watches": rm.get("watches"),
                      "scan_history": rm.get("scan_history")})
    except Exception:
        pass
    try:
        run_queue, _ = _sm_engine()
        pst = run_queue.status()
        state["queue"] = pst.get("queue")
        state["runs"] = [(d.get("result") or {}) for d in (pst.get("done") or [])]
    except Exception:
        pass
    try:
        import sm_lessons
        state["lessons"] = sm_lessons.lessons()
    except Exception:
        state["lessons"] = []
    return state


@sm_router.post("/analyst/ask", dependencies=[Depends(require_operator_identity)])
def sm_analyst_ask(req: SMAnalystRequest):
    """Grounded LLM answer (read-only): assembles the state+corpus+lessons pack
    server-side and calls the Anthropic API. Honest fallback (never empty) on missing
    key/API error. FIREWALL: the analyst path holds NO write capability — it reads
    state and calls the LLM; it never resolves, onboards, banks, or touches 7687."""
    import sm_analyst
    return sm_analyst.answer(req.ask, req.history or [], _analyst_state())


@sm_router.get("/lessons", dependencies=[Depends(require_operator_identity)])
def sm_lessons_list():
    import sm_lessons
    return {"lessons": sm_lessons.lessons()}


@sm_router.post("/lesson/propose", dependencies=[Depends(require_operator_identity)])
def sm_lesson_propose(req: SMLessonProposeRequest):
    """Draft a lesson as PROPOSED (gated). This can NEVER set BANKED — banking is the
    operator's separate endpoint below."""
    import sm_lessons
    return sm_lessons.propose(req.text, source=req.source, proposed_by="operator-request",
                              lesson_id=req.lesson_id)


@sm_router.post("/lesson/bank", dependencies=[Depends(require_operator_identity)])
def sm_lesson_bank(req: SMLessonActionRequest):
    """OPERATOR-ONLY promotion to BANKED (loads into every future grounding pack)."""
    import sm_lessons
    return sm_lessons.bank(req.lesson_id)


@sm_router.post("/lesson/reject", dependencies=[Depends(require_operator_identity)])
def sm_lesson_reject(req: SMLessonActionRequest):
    import sm_lessons
    return sm_lessons.reject(req.lesson_id)


# --- DISCOVERY-ENGINE POWER SWITCH (start/stop/status of the discovery service) ---
# Controls the search-master discovery service ONLY (hardcoded in sm_engine as
# SignalDeltaDiscovery — no service-name parameter exists anywhere in this path, so
# it can never address the production trading engine). Power switch, not a research
# action — it does not bypass the firewall; the engine's gates still gate.
@sm_router.get("/engine/status", dependencies=[Depends(require_operator_identity)])
def sm_engine_status():
    """running | starting | stopping | stopped | not-installed (for the button)."""
    return {"status": engine_status()}


@sm_router.post("/engine/start", dependencies=[Depends(require_operator_identity)])
def sm_engine_start():
    return {"action": "start", "status": engine_start()}


@sm_router.post("/engine/stop", dependencies=[Depends(require_operator_identity)])
def sm_engine_stop():
    return {"action": "stop", "status": engine_stop()}


# Controls the SignalDeltaProxy Windows SERVICE (the surface the portal talks to).
# A power switch, NOT a research/trade action — same auth, no firewall change. The
# restart is what's needed after a deploy so /sm/readmodel serves live 7688 data.
@sm_router.get("/proxy/status", dependencies=[Depends(require_operator_identity)])
def sm_proxy_status():
    """running | starting | stopping | stopped | not-installed (for the button).
    While a restart is in flight this endpoint is briefly unreachable — the button
    reads that as 'restarting' and polls until it answers 'running' again.
    `helper_backed` = the always-on SM_ProxyHelper is up, so a restart works even
    for the first restart after a proxy code change."""
    return {"status": proxy_status(), "helper_backed": helper_available()}


@sm_router.post("/proxy/restart", dependencies=[Depends(require_operator_identity)])
def sm_proxy_restart():
    """Cleanly restart the proxy service via an out-of-tree scheduled task. Returns
    immediately with 'restarting'; the service cycles and comes back with live
    /sm/readmodel. (This is the click that makes 'restart the proxy' terminal-free.)"""
    return {"action": "restart", "status": proxy_restart()}


@sm_router.post("/proxy/stop", dependencies=[Depends(require_operator_identity)])
def sm_proxy_stop():
    return {"action": "stop", "status": proxy_stop()}


@sm_router.post("/proxy/start", dependencies=[Depends(require_operator_identity)])
def sm_proxy_start():
    return {"action": "start", "status": proxy_start()}


@sm_router.get("/health", dependencies=[Depends(require_operator_identity)])
def sm_health():
    """Search-master surface health — reports whether 7688 is provisioned yet, WITHOUT
    leaking secrets. Isolation is structural (this router holds only a 7688 driver)."""
    provisioned = bool(SM_NEO4J_PASSWORD)
    reachable = False
    if provisioned:
        try:
            get_sm_driver().verify_connectivity()
            reachable = True
        except Exception:
            reachable = False
    return {"surface": "search-master", "instance": SM_NEO4J_URI,
            "provisioned": provisioned, "reachable": reachable,
            "branch_isolation": _BRANCH_ISOLATION}
