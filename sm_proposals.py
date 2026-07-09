"""SMProposal store (7688) — monitor/sweep recheck proposals, and the operator's
Approve/Dismiss gate on them.

A proposal is PROPOSED by a class monitor or the all-kills sweep (engine side, as a
durable SMProposal node with the full case). The operator APPROVES (accepts the
recheck — a gated search decision runs it downstream) or DISMISSES it. This module is
the proxy-side read + gate; it sets status only — it never runs a recheck itself.

Firewall: writes proposal status/actor only. No 7687, no trade path, no probe run.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB = os.environ.get("SM_NEO4J_DATABASE", "neo4j")
_ISO = "NOT n:KCCNode AND NOT n:KTMNode"
_driver_cache = None
_DECISION = {"approve": "APPROVED", "dismiss": "DISMISSED"}


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
        _driver_cache = GraphDatabase.driver(uri, auth=(os.environ.get("SM_NEO4J_USER", "neo4j"), pw),
                                             notifications_min_severity="OFF")
    return _driver_cache


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pending() -> list[dict[str, Any]]:
    d = _driver()
    with d.session(database=_DB, default_access_mode="READ") as s:
        return [dict(r["n"]) for r in s.run(
            f"MATCH (n:SMProposal) WHERE {_ISO} AND n.status='PENDING' "
            "RETURN n ORDER BY n.created_at DESC")]


def resolve(proposal_id: str, decision: str, *, actor: str = "operator") -> dict[str, Any]:
    """Operator gate: approve | dismiss -> APPROVED | DISMISSED, stamped with the actor.
    Idempotent-safe: only a PENDING proposal transitions (an already-decided one is
    returned unchanged, never silently re-flipped)."""
    status = _DECISION.get((decision or "").lower())
    if not status:
        return {"id": proposal_id, "error": f"unknown decision {decision!r} (approve|dismiss)"}
    d = _driver()
    with d.session(database=_DB) as s:
        rec = s.run(
            f"MATCH (n:SMProposal {{id:$id}}) WHERE {_ISO} "
            "SET n.status = CASE WHEN n.status='PENDING' THEN $st ELSE n.status END, "
            "    n.resolved_by = CASE WHEN n.status=$st THEN $actor ELSE n.resolved_by END, "
            "    n.resolved_at = CASE WHEN n.status=$st THEN $now ELSE n.resolved_at END "
            "RETURN n.status AS st, n.resolved_by AS by",
            id=proposal_id, st=status, actor=actor, now=_now()).single()
    if not rec:
        return {"id": proposal_id, "status": "not-found"}
    return {"id": proposal_id, "status": rec["st"], "resolved_by": rec["by"]}
