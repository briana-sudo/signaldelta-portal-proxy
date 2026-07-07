"""Discovery-engine POWER SWITCH — start/stop/status of the SEARCH-MASTER discovery
service (SignalDeltaDiscovery), for the Discovery portal's topbar button.

DENY-BY-CONSTRUCTION FIREWALL: the target service is a HARDCODED module constant.
There is deliberately NO parameter, no env override, and no argument on any control
function or route by which a caller could address a different service. A capability
that doesn't exist can't be misrouted — the console can never reach the production
trading engine. (Mirrors SM_ProxyHelper, which is restart-only and hardcoded to
SignalDeltaProxy.)

This controls the SERVICE (a power switch); it does NOT bypass the research
firewall. Starting the discovery service just lets the search-master loop run its
gated cycle; stopping it turns the loop off. Nothing here grades, decides, trades,
or resolves a gate. (Same `require_operator_identity` auth as every other /sm/*.)
"""
from __future__ import annotations

import json as _json
import subprocess
import time as _time
from pathlib import Path as _Path
from typing import Any, Callable

# HARDCODED — the ONLY service this module can address. No env override, no default
# to any other service. Changing the target requires editing this line (a code +
# review change), not flipping an env var or passing an argument.
DISCOVERY_SERVICE = "SignalDeltaDiscovery"
_TIMEOUT = 20
# the discovery worker's OWN heartbeat — read-only; how a restart is VERIFIED (a new pid),
# never assumed. Reading it grants NO service-control capability (deny-by-construction holds).
_STATE_FILE = _Path(r"C:\SignalDelta_Local\searchmaster\state\engine_service.json")


def _sc(action: str) -> subprocess.CompletedProcess:
    """Run `sc.exe <action> SignalDeltaDiscovery`. The service name is fixed here —
    the ONLY caller-controlled input is the action verb (query/start/stop), never a
    service name."""
    return subprocess.run(["sc.exe", action, DISCOVERY_SERVICE],
                          capture_output=True, text=True, timeout=_TIMEOUT)


def classify_state(text: str) -> str:
    """Pure: map `sc query` output → the button's status vocabulary."""
    if "1060" in text or "does not exist" in text:
        return "not-installed"
    if "START_PENDING" in text:
        return "starting"
    if "STOP_PENDING" in text:
        return "stopping"
    if "RUNNING" in text:
        return "running"
    if "STOPPED" in text:
        return "stopped"
    return "unknown"


def engine_status() -> str:
    """running | starting | stopping | stopped | not-installed. Reads the live
    SignalDeltaDiscovery state (`sc query`) — the source of truth for the button."""
    try:
        out = _sc("query")
    except Exception:
        return "unknown"
    return classify_state((out.stdout or "") + (out.stderr or ""))


def _worker_state() -> dict[str, Any]:
    """The discovery worker's OWN heartbeat (pid + running commit) — how a restart is VERIFIED
    (a NEW pid + advanced stamp), not assumed. Read-only; {} on any read failure."""
    try:
        d = _json.loads(_STATE_FILE.read_text(encoding="utf-8")) or {}
        return {"pid": d.get("pid"), "commit": d.get("commit")}
    except Exception:
        return {"pid": None, "commit": None}


def _wait_until(pred: Callable[[], bool], timeout_s: float, poll_s: float = 0.5) -> bool:
    """Poll pred() until true or the budget runs out. Bounded so a hung service REFUSES
    (returns False) rather than hanging the request forever."""
    left = timeout_s
    while left > 0:
        if pred():
            return True
        _time.sleep(poll_s)
        left -= poll_s
    return pred()


def engine_start() -> str:
    """Start the discovery service and VERIFY it reaches RUNNING (bounded) — returns the
    SETTLED status, not a mid-flight START_PENDING guess (DEF-030)."""
    st = engine_status()
    if st == "not-installed":
        return "not-installed"
    if st == "running":
        return st
    try:
        _sc("start")
    except Exception:
        return "unknown"
    _wait_until(lambda: engine_status() == "running", 15)
    return engine_status()


def engine_stop() -> str:
    """Stop the discovery service and VERIFY it reaches STOPPED (bounded) — the SETTLED
    status, not a mid-flight STOP_PENDING guess (DEF-030)."""
    st = engine_status()
    if st == "not-installed":
        return "not-installed"
    if st == "stopped":
        return st
    try:
        _sc("stop")
    except Exception:
        return "unknown"
    _wait_until(lambda: engine_status() in ("stopped", "not-installed"), 15)
    return engine_status()


def _result(ok: bool, status: str, hop: str | None, reason: str | None,
            before: dict, after: dict | None = None) -> dict[str, Any]:
    after = after or {}
    return {"ok": ok, "status": status, "hop": hop, "reason": reason,
            "old_pid": before.get("pid"), "new_pid": after.get("pid"),
            "old_commit": before.get("commit"), "new_commit": after.get("commit"),
            "service": DISCOVERY_SERVICE, "action": "restart"}


def engine_restart() -> dict[str, Any]:
    """VERIFY-OR-REFUSE restart (DEF-030). Cycle SignalDeltaDiscovery and CONFIRM the worker
    actually re-spawned (a NEW pid) before reporting success — never 'restarting' on hope.
    Returns {ok, status, hop, reason, old_pid, new_pid, old_commit, new_commit, service}.
    The proxy stays alive throughout (it cycles a DIFFERENT service), so it CAN verify — this
    is NOT the proxy-self-restart case. Still hard-pinned to DISCOVERY_SERVICE: every _sc()
    call names the constant; no code path here can name SignalDeltaEngine (trading)."""
    if engine_status() == "not-installed":
        return _result(False, "not-installed", "install",
                       "SignalDeltaDiscovery is not installed — run Setup Discovery once.", {})
    before = _worker_state()

    # HOP 1 — STOP, verified it reaches STOPPED
    try:
        r = _sc("stop")
    except Exception as e:
        return _result(False, engine_status(), "stop", f"sc stop raised: {type(e).__name__}: {e}", before)
    if not _wait_until(lambda: engine_status() in ("stopped", "not-installed"), 12):
        return _result(False, engine_status(), "stop",
                       f"service did not reach STOPPED within 12s "
                       f"(sc stop exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:140]})", before)

    # HOP 2 — START, verified it reaches RUNNING
    try:
        r2 = _sc("start")
    except Exception as e:
        return _result(False, engine_status(), "start", f"sc start raised: {type(e).__name__}: {e}", before)
    if not _wait_until(lambda: engine_status() == "running", 20):
        return _result(False, engine_status(), "start",
                       f"service did not reach RUNNING within 20s "
                       f"(sc start exit {r2.returncode}: {(r2.stderr or r2.stdout or '').strip()[:140]})", before)

    # HOP 3 — the WORKER actually re-spawned: a NEW pid in the heartbeat (not the old one)
    if not _wait_until(lambda: _worker_state().get("pid") not in (None, before.get("pid")), 20):
        return _result(False, "running", "cycle",
                       f"service reports RUNNING but the worker pid is still {before.get('pid')} — "
                       f"it did not re-spawn (stale heartbeat / start was a no-op).", before, _worker_state())

    return _result(True, "running", None, None, before, _worker_state())
