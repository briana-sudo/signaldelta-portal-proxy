"""Server-side discovery analyst — a REAL LLM grounded in live 7688 state + the
knowledge corpus + banked lessons (the same inputs the lead reasons over).

READ-ONLY BY CONSTRUCTION: this module imports NO write path — no run_queue.enqueue,
no resolve/onboard/engine control, no secrets store, no lesson-bank. It reads the
corpus files, reads the state the caller passes in, and makes exactly ONE outbound
call: the Anthropic API. It reasons and explains; it never grades, decides, acts, or
writes. The API key is read server-side (env / .env) and used only as the API auth
header — it never enters the pack, a response, or the browser.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_ROOT = Path(r"C:\SignalDelta_Local")
_SEED = _ROOT / "searchmaster" / "seed" / "ENGINE_SEED_STATE.md"
_CORPUS = _ROOT / "searchmaster" / "corpus" / "ABANDONMENT_CORPUS_canonical_v0_2.md"
_MODEL = os.environ.get("SM_ANALYST_MODEL", "claude-sonnet-4-6")
_MAX_CHARS = 30000                                    # cap each corpus doc in the pack

_SYSTEM = """You are the SignalDelta DISCOVERY ANALYST. You REASON over and EXPLAIN the engine's live state and its knowledge corpus. You are structurally READ-ONLY.

HARD ROLE RULES:
- You NEVER grade a result, NEVER decide, NEVER instruct an action, NEVER trade, NEVER write anything. Those are not yours: DECISIONS route to the operator; a RE-GRADE of a killed result routes to deliberate-review. Say so, in your own words, when asked to decide or re-grade.
- Ground every claim in the STATE or CORPUS provided below. Cite which board item / watch / scan / run result / corpus predicate / banked lesson you used.
- If something is not in your state or corpus, say plainly "I don't know — that's not in my state" and stop. NEVER invent numbers, edges, or facts. Prefer the engine's own numbers (edge, t, n, gate, date range, universe).
- You never touch the trading engine (7687). You are the research analyst only.
Be concise and concrete."""

_FALLBACK = ("I can't answer that with the analyst LLM right now (the server-side key or API is unavailable). "
             "I can still answer from the live state directly — try: what's runnable now, the watches, "
             "the recent scans, the board, or ask me to export something.")


def _anthropic_key() -> str | None:
    """Read the key from the SignalDeltaProxy SERVICE ENV ONLY (os.environ). There is
    NO .env file read here — deny-by-construction, so this module can never open the
    trading engine's .env. Key absent → the caller returns the honest fallback.
    (`Setup Proxy Key.bat` injects the key into the proxy service env, machine-side.)"""
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("anthropic_api_key")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:_MAX_CHARS]
    except Exception:
        return ""


def assemble_pack(state: dict[str, Any]) -> str:
    """Build the grounding pack from live state + corpus + banked lessons. No secrets."""
    board = [{k: b.get(k) for k in ("item_id", "title", "kind", "ev", "status", "disposition", "components")}
             for b in (state.get("board") or [])]
    runs = [{k: r.get(k) for k in ("recipe_id", "disposition", "t", "n", "edge_pct_per_day", "gate_pass", "window", "universe")}
            for r in (state.get("runs") or [])]
    banked = [l for l in (state.get("lessons") or []) if l.get("status") == "BANKED"]
    parts = [
        "## LIVE ENGINE STATE (7688)",
        "board (with per-component states): " + json.dumps(board, default=str),
        "watches (revival): " + json.dumps(state.get("watches") or [], default=str),
        "scan_history (recent): " + json.dumps((state.get("scan_history") or [])[:3], default=str),
        "recent run results (edge/t/n/gate/date-range/universe): " + json.dumps(runs, default=str),
        "queue: " + json.dumps(state.get("queue") or [], default=str),
        "\n## BANKED LESSONS (operator-approved; load into every ask)",
        json.dumps(banked, default=str) if banked else "(none banked yet)",
        "\n## SEED STATE (retained / killed / queue / kill-regions)",
        _read(_SEED),
        "\n## ABANDONMENT CORPUS (candidate-vetting predicates A1–A38)",
        _read(_CORPUS),
    ]
    return "\n\n".join(parts)


def _call_anthropic(system: str, history: list[dict], question: str, key: str) -> str:
    msgs = [{"role": ("assistant" if m.get("role") == "analyst" else "user"),
             "content": str(m.get("text", ""))[:4000]}
            for m in (history or []) if m.get("text")][-8:]
    msgs.append({"role": "user", "content": question})
    body = {"model": _MODEL, "max_tokens": 1024, "system": system, "messages": msgs}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def answer(question: str, history: list[dict], state: dict[str, Any]) -> dict[str, Any]:
    """Grounded LLM answer. Honest fallback (never an empty shell) on missing key or
    API error. Returns {kind, explanation, grounded, model}."""
    key = _anthropic_key()
    if not key:
        return {"kind": "EXPLAIN", "explanation": _FALLBACK, "grounded": False, "reason": "no-key"}
    try:
        system = _SYSTEM + "\n\n" + assemble_pack(state)
        text = _call_anthropic(system, history, question, key)
        if not text:
            return {"kind": "EXPLAIN", "explanation": _FALLBACK, "grounded": False, "reason": "empty"}
        return {"kind": "EXPLAIN", "explanation": text, "grounded": True, "model": _MODEL}
    except urllib.error.HTTPError as e:
        return {"kind": "EXPLAIN", "explanation": _FALLBACK, "grounded": False, "reason": f"api-{e.code}"}
    except Exception as e:
        return {"kind": "EXPLAIN", "explanation": _FALLBACK, "grounded": False, "reason": type(e).__name__}
