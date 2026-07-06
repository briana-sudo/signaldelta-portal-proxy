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

import subprocess

# HARDCODED — the ONLY service this module can address. No env override, no default
# to any other service. Changing the target requires editing this line (a code +
# review change), not flipping an env var or passing an argument.
DISCOVERY_SERVICE = "SignalDeltaDiscovery"
_TIMEOUT = 20


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


def engine_start() -> str:
    """Start the discovery service (idempotent). Returns the resulting status."""
    st = engine_status()
    if st == "not-installed":
        return "not-installed"
    if st in ("running", "starting"):
        return st
    try:
        _sc("start")                          # returns immediately with START_PENDING
    except Exception:
        return "unknown"
    return engine_status()


def engine_stop() -> str:
    """Stop the discovery service cleanly (NSSM → graceful shutdown). Idempotent."""
    st = engine_status()
    if st == "not-installed":
        return "not-installed"
    if st in ("stopped", "stopping"):
        return st
    try:
        _sc("stop")
    except Exception:
        return "unknown"
    return engine_status()


def engine_restart() -> str:
    """Restart the discovery service to LOAD CURRENT CODE (a stale worker → fresh). Runs in
    a background thread so the HTTP response returns before the cycle finishes; the button
    polls status back to 'running'. HARD-PINNED by construction: every service action here
    goes through _sc(), whose service name is the DISCOVERY_SERVICE constant — there is no
    code path in this module that can name SignalDeltaEngine (the trading engine)."""
    import threading
    import time

    def _do():
        try:
            _sc("stop")
            for _ in range(20):                   # wait for STOPPED before start (max ~10s)
                if engine_status() in ("stopped", "not-installed"):
                    break
                time.sleep(0.5)
            _sc("start")
        except Exception:
            pass
    if engine_status() == "not-installed":
        return "not-installed"
    threading.Thread(target=_do, daemon=True).start()
    return "restarting"
