"""Search-master ENGINE POWER SWITCH — start/stop/status of the SignalDeltaEngine
Windows service, for the Discovery portal's topbar button.

This controls the SERVICE (a power switch); it does NOT bypass the research
firewall. Starting the engine service just lets the engine run its gated loop;
stopping it turns the loop off. Nothing here grades, decides, trades, or resolves
a gate — the engine's operator gates still gate every research action. (Same
`require_operator_identity` auth as every other /sm/* endpoint.)
"""
from __future__ import annotations

import os
import subprocess

ENGINE_SERVICE = os.environ.get("SM_ENGINE_SERVICE", "SignalDeltaEngine")
_TIMEOUT = 20


def _sc(*args: str) -> subprocess.CompletedProcess:
    # sc.exe is the Windows service controller; the proxy service account (NSSM
    # LocalSystem) can start/stop another service.
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=_TIMEOUT)


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
    service state (`sc query`) — the source of truth for the button."""
    try:
        out = _sc("query", ENGINE_SERVICE)
    except Exception:
        return "unknown"
    return classify_state((out.stdout or "") + (out.stderr or ""))


def engine_start() -> str:
    """Start the engine service (idempotent — already-running is fine). Returns the
    resulting status (typically 'starting' → 'running')."""
    st = engine_status()
    if st == "not-installed":
        return "not-installed"
    if st in ("running", "starting"):
        return st
    try:
        _sc("start", ENGINE_SERVICE)          # returns immediately with START_PENDING
    except Exception:
        return "unknown"
    return engine_status()


def engine_stop() -> str:
    """Stop the engine service cleanly (NSSM turns this into the graceful shutdown
    the run_service loop handles). Idempotent — already-stopped is fine."""
    st = engine_status()
    if st == "not-installed":
        return "not-installed"
    if st in ("stopped", "stopping"):
        return st
    try:
        _sc("stop", ENGINE_SERVICE)
    except Exception:
        return "unknown"
    return engine_status()
