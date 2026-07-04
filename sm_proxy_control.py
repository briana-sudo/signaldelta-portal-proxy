"""Search-master PROXY POWER SWITCH — status/restart of the SignalDeltaProxy
Windows service, for the Discovery portal's topbar button.

Why a scheduled task and not `sc stop` inline: this endpoint LIVES IN the proxy
service. Restarting the service from inside it would kill the process (and, under
NSSM's job object, any child it spawned) before the restart could finish. So a
restart is handed to a one-shot Windows Scheduled Task that runs OUTSIDE the
proxy's process tree — it survives the stop half and issues the start.

This controls the SERVICE only (a power switch). It does NOT bypass the research
firewall, touch 7688 data, or open a trade path. Same `require_operator_identity`
auth as every other /sm/* endpoint.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

PROXY_SERVICE = os.environ.get("SM_PROXY_SERVICE", "SignalDeltaProxy")
RESTART_TASK = os.environ.get("SM_PROXY_RESTART_TASK", "SM_ProxyRestart")
_NSSM = os.environ.get("SM_NSSM_PATH", r"C:\SignalDelta_Local\tools\nssm.exe")
_RESTART_CMD = Path(__file__).with_name("restart_proxy.cmd")
# the always-on helper service (SM_ProxyHelper) — the PRIMARY restart path now
_HELPER_URL = os.environ.get("SM_HELPER_URL", "http://127.0.0.1:8199")
_HELPER_TOKEN = os.environ.get("SM_HELPER_TOKEN")
_TIMEOUT = 25


def _sc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=_TIMEOUT)


def classify_state(text: str) -> str:
    """Pure: `sc query` output → the button's status vocabulary (shared with the
    engine switch so the topbar renders both identically)."""
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


def proxy_status() -> str:
    """running | starting | stopping | stopped | not-installed. Note: while a
    restart is mid-flight the whole surface (incl. this endpoint) is briefly down —
    the button treats an unreachable status as 'restarting' until it answers again."""
    try:
        out = _sc("query", PROXY_SERVICE)
    except Exception:
        return "unknown"
    return classify_state((out.stdout or "") + (out.stderr or ""))


def _ensure_restart_task() -> None:
    """Register (idempotently) the one-shot SYSTEM task that runs the on-disk restart
    script. Created outside the proxy's NSSM job so it survives the stop half."""
    subprocess.run(
        ["schtasks", "/Create", "/TN", RESTART_TASK, "/TR", str(_RESTART_CMD),
         "/SC", "ONCE", "/ST", "23:59", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"],
        capture_output=True, text=True, timeout=_TIMEOUT)


def _call_helper(path: str) -> bool:
    """Ask the always-on SM_ProxyHelper to cycle the proxy. Returns True if the
    helper accepted the request. The helper is a separate service, so it restarts
    the proxy from OUTSIDE the proxy's process tree — clean and code-independent."""
    try:
        headers = {"Content-Type": "application/json"}
        if _HELPER_TOKEN:
            headers["X-Helper-Token"] = _HELPER_TOKEN
        req = urllib.request.Request(f"{_HELPER_URL}{path}", data=b"{}", headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status in (200, 202)
    except Exception:
        return False


def helper_available() -> bool:
    try:
        with urllib.request.urlopen(f"{_HELPER_URL}/helper/health", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def proxy_restart() -> str:
    """Restart the proxy service and return immediately ('restarting').

    PRIMARY: hand off to the always-on SM_ProxyHelper (out-of-tree, survives the
    proxy dying, works even mid-code-change). FALLBACK: the one-shot scheduled task,
    if the helper isn't installed yet. Either way the response flushes before the
    proxy drops, then it comes back with live /sm/readmodel."""
    st = proxy_status()
    if st == "not-installed":
        return "not-installed"
    if _call_helper("/helper/restart"):
        return "restarting"
    # fallback — no helper yet: the out-of-tree scheduled task
    _ensure_restart_task()
    subprocess.run(["schtasks", "/Run", "/TN", RESTART_TASK],
                   capture_output=True, text=True, timeout=_TIMEOUT)
    return "restarting"


def proxy_stop() -> str:
    """Stop the service (scheduled, out-of-tree — the surface goes down; the button
    does not rely on this, Restart is the operation). Idempotent."""
    st = proxy_status()
    if st in ("stopped", "stopping", "not-installed"):
        return st
    _ensure_restart_task()  # the task file also carries a stop path if invoked with an arg
    subprocess.run(["schtasks", "/Run", "/TN", RESTART_TASK],
                   capture_output=True, text=True, timeout=_TIMEOUT)
    return "restarting"


def proxy_start() -> str:
    """Start the service directly (harmless — only reachable when the surface is up,
    which means it's already running; provided for parity)."""
    st = proxy_status()
    if st in ("running", "starting", "not-installed"):
        return st
    try:
        _sc("start", PROXY_SERVICE)
    except Exception:
        return "unknown"
    return proxy_status()
