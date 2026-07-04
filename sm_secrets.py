"""Search-master surface — the SERVER-SIDE SECRETS STORE (spec §9, dispatch §4).

A write-only store for (a) onboarding data-source credentials and (b) the
Cloudflare API token. The read / query / analyst layers CANNOT read it; it never
returns a value — it references only "configured" (a boolean/watermark). Code
builds the store + the write path; the OPERATOR supplies the values (a pasted key
never enters this build's context — the store is populated at runtime).

This is the proxy-side analog of the 3d-i ``SecretsStore``: same discipline
(configured-only, ``__repr__`` never renders values), sited where the proxy's own
credentials live so a secret is out-of-band from the graph, the browser, and the
analyst.
"""
from __future__ import annotations

import os
from pathlib import Path


class SecretsStore:
    """Write-only server-side secrets. ``set`` writes; ``configured`` reports
    presence only; there is deliberately NO accessor that returns a value to the
    read/query/analyst layers. A value is read ONLY by the server-side write path
    that needs it to run (e.g. the fetcher), via ``_use`` — never surfaced up."""

    def __init__(self, backing_dir: str | None = None):
        # optional file backing (0600-intent) for persistence across restarts;
        # in-memory by default. Values are never logged or returned either way.
        self._mem: dict[str, str] = {}
        self._dir = Path(backing_dir) if backing_dir else None
        if self._dir:
            self._dir.mkdir(parents=True, exist_ok=True)

    def set(self, name: str, value: str) -> None:
        """Store a secret VALUE (operator-supplied at runtime). Never logged."""
        self._mem[name] = value
        if self._dir:
            p = self._dir / f"{name}.secret"
            p.write_text(value, encoding="utf-8")
            try:
                os.chmod(p, 0o600)                # best-effort tighten (POSIX; no-op on Windows ACLs)
            except OSError:
                pass

    def configured(self, name: str) -> bool:
        """Presence only — the query/analyst layers use THIS, never a value."""
        if name in self._mem:
            return True
        if self._dir and (self._dir / f"{name}.secret").exists():
            return True
        return False

    def _use(self, name: str) -> str | None:
        """Server-side-only value read for the write path that must run with it
        (e.g. an onboarding fetcher). NOT exposed to any read/query/analyst code
        path — those get ``configured`` only. Underscore-private by contract."""
        if name in self._mem:
            return self._mem[name]
        if self._dir:
            p = self._dir / f"{name}.secret"
            if p.exists():
                return p.read_text(encoding="utf-8")
        return None

    def __repr__(self) -> str:                    # never render values (log-safe)
        keys = sorted(set(self._mem) | (
            {p.stem for p in self._dir.glob("*.secret")} if self._dir else set()))
        return f"SecretsStore(configured={keys})"
