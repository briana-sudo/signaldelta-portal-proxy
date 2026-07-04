"""Search-master (7688) read path — the Cypher QUERY ALLOWLIST (spec §1.1 Rev 3.3).

WHITELIST / DENY-BY-DEFAULT — the load-bearing pin. The proxy PARSES the Cypher and
admits ONLY read-shaped queries; anything unrecognized, unparseable, or not on the
read list is REJECTED. This is deliberately NOT a blacklist of write clauses — a
blacklist (CREATE/MERGE/SET/DELETE/...) is bypassable by comment tricks, casing,
procedure aliases, or a future write clause, so it would be a false floor. The
boundary is: admit only what is provably read-shaped, reject everything else.

Two conditions ride with it (§1.1 Rev 3.3):
  (a) the MCP NL→query path emits PARAMETERIZED Cypher through this SAME gate —
      operator text is never concatenated into a query; values are parameters.
  (b) a rejection is a DRIFT SIGNAL — an attempted write on the read path is logged
      and routed to the context-monitor (trading-engine-write / gating detector).

The allowlist is layer (2) of the three-layer Community read-only boundary
(read-mode sessions · this allowlist · the 3d-i ReadOnlyViolation wrapper). §6
instance-isolation (7688-only pool) is the stronger, separate boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- the READ allowlist: the ONLY clause keywords that may appear ------------
# Admit only these. A query composed solely of these (starting with a read
# leader) is read-shaped; anything else is rejected by deny-by-default.
READ_CLAUSE_KEYWORDS = frozenset({
    "MATCH", "OPTIONAL", "WHERE", "RETURN", "WITH", "UNWIND",
    "ORDER", "BY", "SKIP", "LIMIT", "DISTINCT", "AS", "YIELD",
    "UNION", "ALL", "CALL",                       # CALL is guarded (read subquery / read proc only)
})
# a query must BEGIN with one of these read leaders (a write query begins with
# CREATE/MERGE/... and is rejected here before any keyword-set check).
READ_LEADERS = frozenset({"MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN", "CALL"})

# The language's clause vocabulary — the set of tokens that, if present, MUST be
# an admitted READ clause. Any clause keyword here that is not in the READ set is
# rejected (this is how a write/DDL clause fails: it is NOT on the read list, not
# because it is on a block list). Kept broad; deny-by-default covers the rest.
_ALL_CLAUSE_KEYWORDS = READ_CLAUSE_KEYWORDS | frozenset({
    "CREATE", "MERGE", "SET", "DELETE", "DETACH", "REMOVE", "FOREACH",
    "LOAD", "DROP", "START", "USING", "PERIODIC", "COMMIT",
})

# read-only stored procedures the read path may CALL (deny-by-default: anything
# else, including any apoc.*.write / db write proc, is rejected).
_READ_PROC_ALLOWLIST = frozenset({
    "DB.LABELS", "DB.PROPERTYKEYS", "DB.RELATIONSHIPTYPES", "DB.SCHEMA.VISUALIZATION",
})

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


@dataclass
class AllowlistVerdict:
    allowed: bool
    reason: str


def _strip_noise(cypher: str) -> str:
    """Remove comments and string literals BEFORE keyword scanning, so a clause
    hidden in a comment or a string ('CREATE') cannot smuggle past, and a casing
    trick is neutralized by the caller upper-casing. Returns the code-only text."""
    s = _BLOCK_COMMENT.sub(" ", cypher)
    s = _LINE_COMMENT.sub(" ", s)
    # strip single/double/backtick quoted literals (handle simple escapes)
    out = []
    quote = None
    i = 0
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\" and i + 1 < len(s):
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _balanced(s: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for c in s:
        if c in "([{":
            stack.append(c)
        elif c in pairs:
            if not stack or stack[-1] != pairs[c]:
                return False
            stack.pop()
    return not stack


def is_read_shaped(cypher: str) -> AllowlistVerdict:
    """The whitelist gate. Admit ONLY a read-shaped single statement. Every reject
    path returns a reason (logged as a drift signal by the caller)."""
    if not cypher or not cypher.strip():
        return AllowlistVerdict(False, "empty query")

    code = _strip_noise(cypher)
    if not _balanced(code):
        return AllowlistVerdict(False, "unparseable: unbalanced brackets")

    # single statement only — a trailing `; CREATE ...` second statement is out
    if ";" in code.strip().rstrip(";"):
        return AllowlistVerdict(False, "multiple statements not allowed (single read query only)")

    upper = code.upper()
    tokens = _WORD.findall(upper)
    if not tokens:
        return AllowlistVerdict(False, "no recognizable query tokens")

    # (1) must BEGIN with a read leader — a write query (CREATE/MERGE/...) fails here
    first_kw = next((t for t in tokens if t in _ALL_CLAUSE_KEYWORDS), None)
    if first_kw is None or first_kw not in READ_LEADERS:
        return AllowlistVerdict(False, f"does not begin with a read clause (first clause: {first_kw or 'none'})")

    # (2) every clause keyword present MUST be on the READ allowlist (deny-by-default:
    #     a write/DDL clause is simply not admitted; it is not block-listed)
    for t in tokens:
        if t in _ALL_CLAUSE_KEYWORDS and t not in READ_CLAUSE_KEYWORDS:
            return AllowlistVerdict(False, f"clause {t!r} is not on the read allowlist (deny-by-default)")

    # (3) guard CALL — a read subquery `CALL {` (read-checked by the clause rule
    #     above, since its inner clauses are also scanned) or a read-proc allowlist;
    #     any other CALL <proc>( is rejected.
    for m in re.finditer(r"\bCALL\b\s*(\{)?\s*([A-Z0-9_.]+)?", upper):
        is_subquery = m.group(1) == "{"
        proc = m.group(2)
        if is_subquery:
            continue                              # read subquery — its clauses already gated
        if proc is None or proc not in _READ_PROC_ALLOWLIST:
            return AllowlistVerdict(False, f"CALL {proc or '?'} is not a read-only allowlisted procedure")

    return AllowlistVerdict(True, "read-shaped: admitted")
