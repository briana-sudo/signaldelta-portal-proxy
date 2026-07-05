"""Stamp the proxy's commit into proxy_version.json at DEPLOY/INSTALL time.

WHY: the SignalDeltaProxy service runs as LocalSystem, which is NOT the repo owner
(KCCNode\\brian). Modern git refuses every command in a repo it doesn't own
("detected dubious ownership") unless safe.directory is set, so a runtime
`git rev-parse` inside the service returns nothing → the commit chip reads "unknown".

Fix (same pattern as the frontend's version.json): stamp the commit into a file at
deploy time — run by the repo OWNER, where git works (a git post-commit/post-merge
hook, or the Setup script) — and have the service READ the file at runtime. No git
call at runtime. Idempotent; never raises non-zero in a way that would block a commit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_GIT_BINS = ("git", r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe",
             r"C:\Program Files (x86)\Git\cmd\git.exe")
_OUT = os.path.join(_DIR, "proxy_version.json")


def _git(*args: str) -> str | None:
    for g in _GIT_BINS:
        try:
            r = subprocess.run([g, "-C", _DIR, *args], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and (r.stdout or "").strip():
                return r.stdout.strip()
        except Exception:
            continue
    return None


def stamp() -> str | None:
    commit = _git("rev-parse", "--short", "HEAD")
    if not commit:
        return None
    payload = {"commit": commit, "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
               "stamped_at": datetime.now(timezone.utc).isoformat()}
    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return commit


if __name__ == "__main__":
    c = stamp()
    if c:
        print(f"stamped proxy_version.json -> {c}")
    else:
        print("write_proxy_version: git unavailable — not stamped (service keeps prior stamp)", file=sys.stderr)
    sys.exit(0)                                  # NEVER fail a commit/merge because of a stamp
