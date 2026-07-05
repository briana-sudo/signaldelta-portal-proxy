"""SMLesson store (7688) — GATED learning. A lesson is PROPOSED by the analyst (or
on a run's conclusion), and only the OPERATOR's Bank promotes it to BANKED; BANKED
lessons load into every future grounding pack. Reject marks it REJECTED.

GATE BY SEPARATION: `propose` can only write status=PROPOSED (never BANKED); `bank`
is a DISTINCT function the analyst path never imports. The read-only analyst module
(sm_analyst) holds no reference to this module at all — proposing/banking are
operator-surface endpoints, not part of an ask.

Firewall: writes lesson metadata only (id/text/source/status/timestamps). No secrets,
no 7687, no trade path.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB = os.environ.get("SM_NEO4J_DATABASE", "neo4j")
_ISO = "NOT n:KCCNode AND NOT n:KTMNode"
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
        _driver_cache = GraphDatabase.driver(uri, auth=(os.environ.get("SM_NEO4J_USER", "neo4j"), pw),
                                             notifications_min_severity="OFF")
    return _driver_cache


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def propose(text: str, source: str = "", proposed_by: str = "analyst",
            lesson_id: str | None = None) -> dict[str, Any]:
    """Draft a lesson. Writes status=PROPOSED — NEVER BANKED. Idempotent on lesson_id."""
    lid = lesson_id or f"lesson-{abs(hash((text, source))) % (10 ** 10)}"
    d = _driver()
    with d.session(database=_DB) as s:
        s.run("MERGE (n:SMLesson {id:$i}) "
              "ON CREATE SET n.id=$i, n.text=$t, n.source=$src, n.proposed_by=$pb, "
              "n.status='PROPOSED', n.proposed_at=$now "
              "ON MATCH SET n.text=$t, n.source=$src",     # re-propose refreshes text, keeps status unless still proposed
              i=lid, t=text, src=source, pb=proposed_by, now=_now())
    return {"id": lid, "status": "PROPOSED"}


def bank(lesson_id: str) -> dict[str, Any]:
    """OPERATOR promotes a proposed lesson to BANKED. Only path that sets BANKED.
    Banking CLEARS any stale superseded_by/superseded_at — a BANKED lesson is the
    ACTIVE one and must never point at a superseder (self-heals the re-terminus bug
    where a later PROPOSED draft stamped superseded_by onto an already-banked lesson)."""
    d = _driver()
    with d.session(database=_DB) as s:
        rec = s.run(f"MATCH (n:SMLesson {{id:$i}}) WHERE {_ISO} "
                    "SET n.status='BANKED', n.banked_at=$now, "
                    "n.superseded_by=null, n.superseded_at=null RETURN n.status AS st",
                    i=lesson_id, now=_now()).single()
    return {"id": lesson_id, "status": rec["st"] if rec else "not-found"}


def validate_banked() -> list[dict[str, Any]]:
    """Integrity guard: a BANKED lesson must NOT carry a superseded_by — it IS the
    active lesson. Returns any violators (empty list = clean). A re-terminus that only
    supersedes PROPOSED can't create a violator; this catches stale data + regressions."""
    d = _driver()
    with d.session(database=_DB, default_access_mode="READ") as s:
        return [dict(r["n"]) for r in s.run(
            f"MATCH (n:SMLesson) WHERE {_ISO} AND n.status='BANKED' "
            "AND n.superseded_by IS NOT NULL RETURN n")]


def validate_lineage() -> list[dict[str, Any]]:
    """Ruling h — integrity guard so the dangling state can't recur: a SUPERSEDED lesson
    MUST carry a superseded_by that resolves to a known lesson. Returns violators (empty =
    clean). The 4 historical dangling lessons were retracted by the ruling executor; this
    catches any regression before it reaches the analyst's lineage."""
    d = _driver()
    with d.session(database=_DB, default_access_mode="READ") as s:
        known = {dict(r["n"])["id"] for r in s.run(
            f"MATCH (n:SMLesson) WHERE {_ISO} RETURN n")}
        out = []
        for r in s.run(f"MATCH (n:SMLesson) WHERE {_ISO} AND n.status='SUPERSEDED' RETURN n"):
            n = dict(r["n"])
            sby = n.get("superseded_by")
            if not sby or sby not in known:
                out.append({"id": n["id"], "superseded_by": sby,
                            "violation": "empty" if not sby else "unresolvable"})
        return out


def unbank(lesson_id: str) -> dict[str, Any]:
    """OPERATOR retracts a banked lesson → RETRACTED: removed from the grounding pack
    (assemble_pack loads only BANKED), history kept. Only a BANKED lesson can unbank."""
    d = _driver()
    with d.session(database=_DB) as s:
        rec = s.run(f"MATCH (n:SMLesson {{id:$i}}) WHERE {_ISO} AND n.status='BANKED' "
                    "SET n.status='RETRACTED', n.retracted_at=$now RETURN n.status AS st",
                    i=lesson_id, now=_now()).single()
    return {"id": lesson_id, "status": rec["st"] if rec else "not-banked"}


def reject(lesson_id: str) -> dict[str, Any]:
    d = _driver()
    with d.session(database=_DB) as s:
        rec = s.run(f"MATCH (n:SMLesson {{id:$i}}) WHERE {_ISO} "
                    "SET n.status='REJECTED', n.rejected_at=$now RETURN n.status AS st",
                    i=lesson_id, now=_now()).single()
    return {"id": lesson_id, "status": rec["st"] if rec else "not-found"}


def lessons() -> list[dict[str, Any]]:
    d = _driver()
    with d.session(database=_DB, default_access_mode="READ") as s:
        return [dict(r["n"]) for r in s.run(
            f"MATCH (n:SMLesson) WHERE {_ISO} RETURN n ORDER BY n.proposed_at DESC")]
