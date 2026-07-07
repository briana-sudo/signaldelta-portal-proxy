"""ProxyRestartNode provenance (7688) — every proxy restart is logged with its ACTOR.

Same discipline as a run (SMRunRequest: enqueued/started/finished): a restart is
recorded BEFORE it is dispatched (who, when, via, from→to commit, manual/auto, and
whether the dispatch was accepted), and the loop is CLOSED on the next process start —
the fresh proxy stamps came_back_at + running_commit onto the still-open record
(stamp-on-start, mirrors DEF-027 for the engine). No restart is ever unattributed.

Firewall: writes restart metadata only to 7688. No 7687, no trade path, no secrets
(the auth token is never stored — only an operator identity string + a client IP).
Best-effort by contract: provenance must NEVER block or fail a restart, so every
public call swallows its own errors and returns a status the caller can log.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB = os.environ.get("SM_NEO4J_DATABASE", "neo4j")
_ISO = "NOT n:KCCNode AND NOT n:KTMNode"          # 7688 hygiene filter (shared convention)
_driver_cache = None


def _password() -> str | None:
    pw = os.environ.get("SM_NEO4J_PASSWORD")
    if pw:
        return pw
    envf = Path(r"C:\SignalDelta_Local\signaldelta-portal-proxy\.env")
    if envf.exists():
        for line in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"^\s*SM_NEO4J_PASSWORD\s*=\s*(.+?)\s*$", line)
            if m:
                return m.group(1)
    return None


def _driver():
    global _driver_cache
    if _driver_cache is None:
        from neo4j import GraphDatabase
        pw = _password()
        if not pw:
            raise RuntimeError("7688 password unset")
        uri = os.environ.get("SM_NEO4J_URI", "bolt://localhost:7688")
        _driver_cache = GraphDatabase.driver(
            uri, auth=(os.environ.get("SM_NEO4J_USER", "neo4j"), pw),
            notifications_min_severity="OFF")
    return _driver_cache


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_VALID_TRIGGER = {"manual", "auto", "unknown"}


def record_restart(*, source: str, actor: str, trigger: str = "unknown",
                   client_ip: str = "", from_commit: str = "",
                   to_commit: str = "") -> dict[str, Any]:
    """Persist a ProxyRestartNode BEFORE dispatch. Returns {restart_id, recorded}.
    Best-effort: on any failure returns recorded=False with the reason (never raises —
    a provenance write must not stop a restart)."""
    rid = uuid.uuid4().hex
    trig = trigger if trigger in _VALID_TRIGGER else "unknown"
    try:
        d = _driver()
        with d.session(database=_DB) as s:
            s.run(
                "CREATE (n:ProxyRestartNode {"
                "  id:$id, source:$source, actor:$actor, trigger:$trigger,"
                "  client_ip:$ip, from_commit:$from_c, to_commit:$to_c,"
                "  status:'DISPATCHED', dispatched_at:$now,"
                "  came_back_at:null, running_commit:null })",
                id=rid, source=source, actor=actor or "unknown", trigger=trig,
                ip=client_ip or "", from_c=from_commit or "", to_c=to_commit or "",
                now=_now())
        return {"restart_id": rid, "recorded": True}
    except Exception as e:                              # provenance is never load-bearing
        return {"restart_id": rid, "recorded": False, "reason": str(e)[:200]}


def mark_result(restart_id: str, result: str) -> None:
    """Stamp the dispatch OUTCOME (dispatched | dispatch-failed) onto the record. A
    dispatch-failed restart never bounces the process, so it will never get a
    stamp-on-start — recording the outcome here keeps it from looking 'still open'."""
    try:
        d = _driver()
        with d.session(database=_DB) as s:
            s.run(f"MATCH (n:ProxyRestartNode {{id:$id}}) WHERE {_ISO} "
                  "SET n.dispatch_result=$r, "
                  "    n.came_back_at = CASE WHEN $r='dispatch-failed' THEN n.dispatched_at ELSE n.came_back_at END,"
                  "    n.status = CASE WHEN $r='dispatch-failed' THEN 'DISPATCH_FAILED' ELSE n.status END",
                  id=restart_id, r=result)
    except Exception:
        pass


def stamp_return(running_commit: str) -> dict[str, Any]:
    """Called on proxy STARTUP: close the most-recent still-open restart record with
    came_back_at + the commit now running. Idempotent-ish (only the newest open record
    is stamped). Best-effort — never blocks startup."""
    try:
        d = _driver()
        with d.session(database=_DB) as s:
            rec = s.run(
                f"MATCH (n:ProxyRestartNode) WHERE {_ISO} "
                "AND n.status='DISPATCHED' AND n.came_back_at IS NULL "
                "WITH n ORDER BY n.dispatched_at DESC LIMIT 1 "
                "SET n.came_back_at=$now, n.running_commit=$c, n.status='COMPLETED' "
                "RETURN n.id AS id, n.actor AS actor, n.trigger AS trigger",
                now=_now(), c=running_commit or "").single()
        return dict(rec) if rec else {"stamped": None}
    except Exception as e:
        return {"stamped": None, "reason": str(e)[:200]}


def restarts(limit: int = 50) -> list[dict[str, Any]]:
    """Read the restart ledger (newest first) — for a future /sm/proxy/restarts view."""
    try:
        d = _driver()
        with d.session(database=_DB, default_access_mode="READ") as s:
            return [dict(r["n"]) for r in s.run(
                f"MATCH (n:ProxyRestartNode) WHERE {_ISO} "
                "RETURN n ORDER BY n.dispatched_at DESC LIMIT $lim", lim=int(limit))]
    except Exception:
        return []
