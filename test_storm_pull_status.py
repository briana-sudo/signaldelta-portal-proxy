"""Contract tests for the Item 10 async-PULL poll query (storm_pull_status).

Hermetic (no Neo4j): asserts the poll is a READ-ONLY whitelist entry that can
NEVER trigger or re-trigger a compute. The ONLY write trigger is /storm_pull
(write-bearer gated); this query just reads the PullJob lifecycle marker the
engine wrote. See the async fire-and-poll design (proxy returns a job_id
immediately, portal polls this until terminal, then refreshes).
"""
import re

from queries import QUERIES, REQUIRED_PARAMS


def _q() -> str:
    return QUERIES["storm_pull_status"]


def test_pull_status_in_read_whitelist_keyed_on_job_id():
    assert "storm_pull_status" in QUERIES
    assert REQUIRED_PARAMS["storm_pull_status"] == ["job_id"]
    assert set(QUERIES) == set(REQUIRED_PARAMS), "whitelist key sets must match"


def test_pull_status_is_read_only_never_writes():
    q = _q().upper()
    # A poll must not mutate the graph: no write clause may appear.
    for kw in (" CREATE ", " MERGE ", " SET ", " DELETE ", " REMOVE ", "DETACH"):
        assert kw not in f" {q} ", f"storm_pull_status must be read-only — found {kw.strip()}"


def test_pull_status_reads_pulljob_with_branch_isolation():
    q = _q()
    assert "PullJob" in q and "$job_id" in q, "must MATCH PullJob by $job_id"
    # tenant isolation: positive :StormNode + the KCC/KTM/Trade exclusion guard
    assert ":StormNode:PullJob" in q
    for foreign in ("KCCNode", "KTMNode", "TradeNode"):
        assert f"NOT n:{foreign}" in q, f"missing exclusion guard for {foreign}"


def test_pull_status_returns_lifecycle_fields():
    q = _q()
    for field in ("state", "circles", "swath_cells", "cluster_id", "error"):
        assert re.search(rf"n\.{field} AS {field}", q), f"poll must return {field}"
