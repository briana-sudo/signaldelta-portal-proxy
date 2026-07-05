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


class SMTerminusLLMRequest(BaseModel):
    system: str
    user: str
    max_tokens: int = 1024


class SMReevaluateRequest(BaseModel):
    item_id: str


class SMCancelRequest(BaseModel):
    item_id: str


class SMDebriefRequest(BaseModel):
    run_id: str                                       # 'Debrief this' on a concluded run
    run: dict[str, Any] | None = None                 # optional inline run (else fetched from 7688)


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


# ── running-commit visibility (RESTART != DEPLOY root fix) ───────────────────
# Capture the git HEAD at IMPORT time = the commit this process is actually running.
# Compared live to the tree HEAD, so "is the new code live?" is answerable directly:
# running != tree  ⇒  the disk has newer code than this process ⇒ STALE (update+restart).
import subprocess as _subp

_PROXY_DIR = os.path.dirname(os.path.abspath(__file__))


# the LocalSystem service PATH often lacks git → try the standard install paths too,
# else running_commit reads null even though the code IS current (the commit chip lie).
_GIT_BINS = ("git", r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe",
             r"C:\Program Files (x86)\Git\cmd\git.exe")


_VERSION_FILE = os.path.join(_PROXY_DIR, "proxy_version.json")


def _git_short(ref: str = "HEAD") -> str | None:
    """DEV-ONLY fallback. In the LocalSystem service this fails (git dubious-ownership,
    LocalSystem != repo owner), which is exactly why the stamp file is the primary
    source. Kept for a developer running the proxy under their own account."""
    for gitexe in _GIT_BINS:
        try:
            r = _subp.run([gitexe, "-C", _PROXY_DIR, "rev-parse", "--short", ref],
                          capture_output=True, text=True, timeout=5)
            out = (r.stdout or "").strip()
            if r.returncode == 0 and out:
                return out
        except Exception:
            continue
    return None


def _stamped_commit() -> str | None:
    """The commit stamped into proxy_version.json at deploy/install time (by the repo
    OWNER, where git works — a post-commit/post-merge hook or Setup). This is the
    PRIMARY source; NO runtime git under LocalSystem."""
    try:
        with open(_VERSION_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("commit") or None
    except Exception:
        return None


def _read_commit() -> str | None:
    return _stamped_commit() or _git_short("HEAD")     # stamp first; git only for dev


_RUNNING_COMMIT = _read_commit()                # frozen at process start (stamp preferred)


_ENGINE_STATE_DIR = r"C:\SignalDelta_Local\searchmaster\state"


def _engine_commit_state() -> dict[str, Any]:
    """The DISCOVERY ENGINE's commit — running (from its heartbeat, stamped at process
    start) vs deployed (its version stamp). Answers 'is the engine current' with a chip,
    never pid-vs-commit-time archaeology. Files only; no runtime git."""
    def _c(fname):
        try:
            with open(os.path.join(_ENGINE_STATE_DIR, fname), encoding="utf-8") as f:
                return (json.load(f) or {}).get("commit")
        except Exception:
            return None
    run_c, tree_c = _c("engine_service.json"), _c("engine_version.json")
    return {"engine_commit": run_c, "engine_tree_commit": tree_c,
            "engine_stale": bool(run_c and tree_c and run_c != tree_c)}


def _commit_state() -> dict[str, Any]:
    # tree = the stamp RE-READ now (the deploy hook rewrites it on each commit), so a
    # commit-without-restart shows stale — all without a runtime git call.
    tree = _read_commit()
    return {"running_commit": _RUNNING_COMMIT, "tree_commit": tree,
            "stale": bool(_RUNNING_COMMIT and tree and _RUNNING_COMMIT != tree),
            "commit_source": "stamp" if _stamped_commit() else ("git" if _RUNNING_COMMIT else "unknown"),
            **_engine_commit_state()}


def _surface_of(parent_or_id: str) -> str:
    """Coverage surface for a run/board id: 'new-search-surface:V-015#V-015-TDF' -> 'V-015'."""
    p = str(parent_or_id or "").split("#")[0]
    return p.split(":")[-1] if ":" in p else p


def _derive_cell_status(grid: list[dict[str, Any]], runs: list[dict[str, Any]]) -> None:
    """MAP LIVENESS — cell status is DERIVED from the component RUN RESULTS via the
    fixed taxonomy AT READ TIME, not read from a stored (mutable, go-stale) cell.
    A surface reads its true state on cold load, before any Re-evaluate: V-015's
    components already carry results in 7688, so re-deriving t/n through the current
    taxonomy paints TESTED-INCONCLUSIVE even if the stored cell says 'whitespace'.
    The generator's intrinsic status (whitespace/gated/occupied) stands for untested
    surfaces. Best-effort: any hiccup leaves the stored status untouched."""
    try:
        if r"C:\SignalDelta_Local" not in _sys.path:
            _sys.path.insert(0, r"C:\SignalDelta_Local")
        from searchmaster.engine import taxonomy
    except Exception:
        return
    by_surface: dict[str, list[str]] = {}
    for r in runs or []:
        if r.get("kind") == "reterminus":
            continue
        res = r.get("result")
        if not isinstance(res, dict) or res.get("t") is None:
            continue
        disp = taxonomy.disposition_for(
            gate_pass=res.get("gate_pass"), t=res.get("t"), n=res.get("n"),
            edge=res.get("edge_pct_per_day"), gate=res.get("gate") or {})
        by_surface.setdefault(_surface_of(r.get("parent") or r.get("item_id")), []).append(disp)
    for cell in grid or []:
        disps = by_surface.get(cell.get("surface"))
        if disps:
            _, status = taxonomy.aggregate_parent(disps)
            cell["status"] = taxonomy.cell_status_for_parent(status)   # DERIVED override


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
        grid = rows("SMGridCell")
        runs = rows("SMRunRequest")
        _derive_cell_status(grid, runs)               # MAP LIVENESS: derived, not stored
        return {
            "board": rows("SMBoardItem"),
            "state": state[0] if state else {},
            "grid": grid,
            "ledger": rows("SMComponent"),
            "kills": rows("SMKill"),
            "gated": rows("SMGatedSurface"),
            "deployed": rows("SMDeployedSignal"),
            # revival monitor state + recheck-scan history (the Timeline's live source)
            "watches": rows("SMWatch"),
            "scan_history": rows("SMScan", order=" ORDER BY n.at DESC"),
            # every run (probe / component / re-terminus) with its stage progress,
            # result, disposition record + report version history — the Run Room source
            "runs": runs,
            # SURVIVOR pipelines (S1–S6) opened on retained candidates
            "candidates": rows("SMCandidate", order=" ORDER BY n.opened_at DESC"),
            # TERMINUS combiner outputs: per-run return streams + pairwise rho graph
            "streams": rows("SMReturnStream"),
            "correlations": [
                {"from": r["a"], "to": r["b"], "rho": r["rho"], "n_overlap": r["n"]}
                for r in session.run(
                    # NOTE: this query aliases nodes a/b (not n), so the branch-isolation
                    # filter must reference a/b — SMReturnStream is SM-only anyway.
                    "MATCH (a:SMReturnStream)-[c:CORRELATES_WITH]->(b:SMReturnStream) "
                    "WHERE NOT a:KCCNode AND NOT a:KTMNode AND NOT b:KCCNode AND NOT b:KTMNode "
                    "RETURN a.id AS a, b.id AS b, c.rho AS rho, c.n_overlap AS n")
            ],
        }


import sys as _sys


def _sm_engine():
    """Import the search-master engine (queue + recipe registry) once."""
    if r"C:\SignalDelta_Local" not in _sys.path:
        _sys.path.insert(0, r"C:\SignalDelta_Local")
    from searchmaster.engine import recipe_registry, run_queue
    return run_queue, recipe_registry


@sm_router.get("/handoff", dependencies=[Depends(require_operator_identity)])
def sm_handoff():
    """LEAD HANDOFF PACK — compose the successor's boot briefing (BOOT_CONTEXT.md) FRESH
    from live 7688 + config + code. READ-ONLY: a VIEW of state, never stored, never a
    mutation. Returns {markdown, manifest, generated_at, provenance, commits, words}."""
    import datetime as _dt
    if r"C:\SignalDelta_Local" not in _sys.path:
        _sys.path.insert(0, r"C:\SignalDelta_Local")
    from searchmaster.engine import handoff as _handoff, audit as _audit

    driver = get_sm_driver()                          # 503 until 7688 provisioned
    with driver.session(database=SM_NEO4J_DATABASE, default_access_mode="READ") as session:
        def rows(label: str, where: str = _BRANCH_ISOLATION) -> list[dict[str, Any]]:
            return [dict(r["n"]) for r in session.run(
                f"MATCH (n:{label}) WHERE {where} RETURN n")]
        killed = rows("SMKill")
        for k in killed:                              # revival-class each (assigned-or-flagged)
            rc = _audit.revival_class({"reason": k.get("reason", ""),
                                       "disposition": k.get("disposition"),
                                       "revival_class": k.get("revival_class")})
            k["revival_class"] = rc["revival_class"] or "UNASSIGNED-FLAG"
        state = {
            "provenance": "live-7688", "killed": killed,
            "retained": rows("SMKill", where=f"{_BRANCH_ISOLATION} AND n.status = 'retained'"),
            "watches": rows("SMWatch"), "lessons": rows("SMLesson"),
            "board": rows("SMBoardItem"), "runs": rows("SMRunRequest"),
            "defects": _handoff.DEFECTS, "standards": _handoff.STANDARDS,
        }
    cs = _commit_state()
    commits = {"proxy": cs.get("running_commit") or "unstamped",
               "engine": cs.get("engine_commit") or "unstamped"}
    generated_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md = _handoff.compose_pack(state, generated_at=generated_at, commits=commits)
    return {"markdown": md, "manifest": _handoff.COMPANION_MANIFEST,
            "generated_at": generated_at, "provenance": state["provenance"],
            "commits": commits, "words": len(md.split())}


@sm_router.get("/ruling-sheet", dependencies=[Depends(require_operator_identity)])
def sm_ruling_sheet():
    """OPERATOR RULING SHEET — the kill-board audit's findings as one decision MD (items
    a–h, four columns each, every item a PROPOSAL). Read-only: serves the committed audit
    artifact; it executes nothing and creates no run/bank/write."""
    path = r"C:\SignalDelta_Local\searchmaster\docs\OPERATOR_RULING_SHEET.md"
    try:
        with open(path, encoding="utf-8") as f:
            md = f.read()
        return {"markdown": md, "words": len(md.split()), "source": "audit-completion-pass"}
    except FileNotFoundError:
        return {"markdown": None, "error": "ruling sheet not generated yet (run the audit with --sheet)"}


@sm_router.post("/debrief", dependencies=[Depends(require_operator_identity)])
def sm_debrief(req: SMDebriefRequest):
    """OPERATOR DEBRIEF — four plain-English voices over a concluded run's grounding.
    READ-ONLY composer: it explains + provokes, writes nothing. The four LLM passes use
    the proxy's own Anthropic key (sm_analyst.raw); an unreachable key yields honest
    per-voice absence, never a heuristic imitation."""
    import sm_analyst
    if r"C:\SignalDelta_Local" not in _sys.path:
        _sys.path.insert(0, r"C:\SignalDelta_Local")
    from searchmaster.engine import debrief as _debrief, build_notes as _bn
    from searchmaster.engine.llm import LLMUnavailable

    # locate the run: inline, else fetch it (+ grounding slices) from live 7688
    run = req.run
    lessons = board = watches = notes = None
    driver = get_sm_driver()
    with driver.session(database=SM_NEO4J_DATABASE, default_access_mode="READ") as s:
        if run is None:
            rec = s.run("MATCH (r:SMRunRequest) WHERE r.item_id = $id OR r.id = $id RETURN r",
                        id=req.run_id).single()
            run = dict(rec["r"]) if rec else None
        def rws(label, key):
            return [dict(r[key]) for r in s.run(f"MATCH (n:{label}) WHERE {_BRANCH_ISOLATION} RETURN n AS {key}")]
        lessons = rws("SMLesson", "n")
        board = rws("SMBoardItem", "n")
        watches = rws("SMWatch", "n")
        notes = _bn.recent_notes(s, 8)
    if run is None:
        return {"error": f"run {req.run_id!r} not found in 7688", "unavailable": [{"voice": "*", "reason": "no such run"}]}

    def _proxy_llm(system: str, user: str, *, max_tokens: int = 700) -> str:
        out = sm_analyst.raw(system, user, max_tokens=max_tokens)
        if not out.get("text"):
            raise LLMUnavailable(out.get("reason") or "no-key")
        return out["text"]

    grounding = _debrief.assemble_grounding(run, lessons=lessons, board=board,
                                            watches=watches, proposals=notes)
    db = _debrief.compose_debrief(grounding, llm_call=_proxy_llm)
    db["run_id"] = req.run_id
    db["markdown"] = _debrief.render_markdown(db)
    return db


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


@sm_router.post("/terminus/llm", dependencies=[Depends(require_operator_identity)])
def sm_terminus_llm(req: SMTerminusLLMRequest):
    """LLM passthrough for the engine's TERMINUS. The discovery service holds NO
    Anthropic key (deny-by-construction) — it POSTs here and the PROXY (which holds
    the key in its service env) makes the one Anthropic call. READ-ONLY: no state or
    graph write, exactly like the analyst path."""
    import sm_analyst
    return sm_analyst.raw(req.system, req.user, req.max_tokens)


@sm_router.post("/reevaluate", dependencies=[Depends(require_operator_identity)])
def sm_reevaluate(req: SMReevaluateRequest):
    """Enqueue a RE-TERMINUS job (the operator's Re-evaluate / deliberate-review click)
    for a concluded board item. The ENGINE re-judges its OWN stored work with the fixed
    taxonomy + LLM; this endpoint only writes a run-REQUEST (the runner does the work,
    streaming to the In-progress tab). FIREWALL: no probe, no buy, no trading instance."""
    try:
        run_queue, _ = _sm_engine()
        return run_queue.enqueue_reterminus(req.item_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"engine unavailable: {e}")


@sm_router.post("/probe/cancel", dependencies=[Depends(require_operator_identity)])
def sm_probe_cancel(req: SMCancelRequest):
    """Operator-initiated abort of a running (or queued) probe. Sets the cancel flag in
    7688; the engine's supervisor polls it and aborts the worker cooperatively → the run
    is ERRORED 'cancelled by operator', the lock released, the item re-approvable. This
    endpoint only sets intent — it never grades, decides, or touches the trading engine."""
    try:
        run_queue, _ = _sm_engine()
        return run_queue.request_cancel(req.item_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"engine unavailable: {e}")


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
def _persist_costing(card_id: str, result: dict[str, Any]) -> bool:
    """PERSIST the pricing result onto the SMGatedSurface node (matched by id OR
    surface_id — the two id fields the seed and the terminus each wrote), so priced
    fields survive refresh/restart instead of living only in client session state.
    Best-effort: a 7688 hiccup leaves the (still-returned) result client-side."""
    if not card_id:
        return False
    import datetime as _dt
    fields = result.get("fields") or {}
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        driver = get_sm_driver()
        with driver.session(database=SM_NEO4J_DATABASE) as s:
            rec = s.run(f"MATCH (n:SMGatedSurface) WHERE {_BRANCH_ISOLATION} "
                        "AND (n.id=$i OR n.surface_id=$i) "
                        "SET n += $f, n.priced=true, n.priced_questions=$q, n.priced_at=$now "
                        "RETURN count(n) AS c",
                        i=card_id, f=fields, q=json.dumps(result.get("questions") or []),
                        now=now).single()
            return bool(rec and rec["c"])
    except Exception:
        return False


@sm_router.post("/research", dependencies=[Depends(require_operator_identity)])
def sm_research(req: SMResearchRequest):
    """Research the real cost of a gated surface (vendor, cost/yr, monthly, terms,
    tiers, what-you-get) and return filled fields + the operator's judgment-call
    questions. PERSISTS the result to 7688 so it survives refresh. FIREWALL:
    researches + fills only — NEVER buys, onboards, or spends; Approve stays the
    operator's. (No onboard/resolve/secrets call in this path.)"""
    import sm_costing
    result = sm_costing.research(req.surface_id, req.surface)
    result["persisted"] = _persist_costing(req.surface_id, result)
    return result


# --- ANALYST (real LLM, grounded) + GATED LEARNING (SMLesson) -----------------
def _analyst_state() -> dict[str, Any]:
    """Assemble the read-only grounding state: live board/watches/scans (7688) +
    recent run results (with date-range/universe) + banked lessons. Reads only."""
    state: dict[str, Any] = {}
    try:
        rm = sm_readmodel()
        state.update({"board": rm.get("board"), "watches": rm.get("watches"),
                      "scan_history": rm.get("scan_history"),
                      # data-needs cards WITH their PERSISTED pricing (so the analyst
                      # speaks from saved truth, not client session state)
                      "data_needs": rm.get("gated")})
    except Exception:
        pass
    try:
        run_queue, _ = _sm_engine()
        pst = run_queue.status()
        state["queue"] = pst.get("queue")
        # each run's result dict, with the error (if any) surfaced as error_message so
        # the analyst sees an errored run AS an error, never a gate FAIL / classified kill.
        state["runs"] = [{**(d.get("result") or {}),
                          "error_message": (d.get("result") or {}).get("error")}
                         for d in (pst.get("done") or [])]
    except Exception:
        pass
    try:
        import sm_lessons
        state["lessons"] = sm_lessons.lessons()
    except Exception:
        state["lessons"] = []
    try:                                              # recent build-notes (engine memory)
        if r"C:\SignalDelta_Local" not in _sys.path:
            _sys.path.insert(0, r"C:\SignalDelta_Local")
        from searchmaster.engine import build_notes
        driver = get_sm_driver()
        with driver.session(database=SM_NEO4J_DATABASE, default_access_mode="READ") as s:
            state["build_notes"] = build_notes.recent_notes(s, 8)
    except Exception:
        state["build_notes"] = []
    return state


@sm_router.post("/analyst/ask", dependencies=[Depends(require_operator_identity)])
def sm_analyst_ask(req: SMAnalystRequest):
    """Grounded LLM answer (read-only): assembles the state+corpus+lessons pack
    server-side and calls the Anthropic API. Honest fallback (never empty) on missing
    key/API error. FIREWALL: the analyst path holds NO write capability — it reads
    state and calls the LLM; it never resolves, onboards, banks, or reaches the trading instance."""
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


@sm_router.post("/lesson/unbank", dependencies=[Depends(require_operator_identity)])
def sm_lesson_unbank(req: SMLessonActionRequest):
    """OPERATOR-ONLY: retract a banked lesson (→ RETRACTED, out of the grounding pack;
    history kept). The inverse of Bank."""
    import sm_lessons
    return sm_lessons.unbank(req.lesson_id)


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
    for the first restart after a proxy code change. running_commit/tree_commit/stale
    make 'is the new code live?' answerable directly (never again only by behavior)."""
    return {"status": proxy_status(), "helper_backed": helper_available(), **_commit_state()}


def _git_update() -> dict[str, Any]:
    """Fast-forward the service tree to the designated deploy branch (SM_DEPLOY_BRANCH,
    default 'main'). Best-effort; ff-only so it never rewrites/loses local state."""
    branch = os.environ.get("SM_DEPLOY_BRANCH", "main")
    try:
        _subp.run(["git", "-C", _PROXY_DIR, "fetch", "--quiet", "origin", branch], timeout=40)
        r = _subp.run(["git", "-C", _PROXY_DIR, "merge", "--ff-only", f"origin/{branch}"],
                      capture_output=True, text=True, timeout=40)
        return {"branch": branch, "ok": r.returncode == 0,
                "detail": ((r.stdout or "") + (r.stderr or "")).strip()[:200], "tree_commit": _git_short("HEAD")}
    except Exception as e:
        return {"branch": branch, "ok": False, "detail": f"{type(e).__name__}: {e}"}


@sm_router.post("/proxy/update-restart", dependencies=[Depends(require_operator_identity)])
def sm_proxy_update_restart():
    """UPDATE & RESTART — the fix for 'restart != deploy'. Fast-forwards the service
    tree to the deploy branch FIRST, then restarts via the helper, so the running
    process picks up the latest code (not just cycles the old code). The topbar's
    running_commit reflects the new commit once it comes back."""
    update = _git_update()
    return {"action": "update-restart", "update": update, "status": proxy_restart()}


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
