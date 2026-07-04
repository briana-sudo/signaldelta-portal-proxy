"""Contract test for the engine_heartbeat query (2026-06-06 heartbeat fix).

Hermetic (no Neo4j): asserts the engine_heartbeat Cypher feeds last_engine_write
from the node types the engine actually writes on a quiet/closed-market day —
AccountStateNode (~30s cadence) and Layer4AnomalyNode (~15min reconcile) — using
each label's CORRECT freshness property, and that every matched label carries the
KCC/KTM branch-isolation exclusion.

Why structural rather than a seeded DB test: the only available Neo4j is the live
production instance, and AccountStateNode is a MERGE'd singleton keyed on
account_id — seeding one in a test would overwrite the real engine heartbeat node.
Runtime behaviour was verified live instead (see the dispatch closeout:
last_engine_write returned AccountStateNode.snapshot_timestamp, ~0.2s old).
"""
import re
from queries import QUERIES


def _heartbeat() -> str:
    return QUERIES["engine_heartbeat"]


def test_accountstatenode_feeds_heartbeat_via_snapshot_timestamp():
    q = _heartbeat()
    assert "AccountStateNode" in q, "AccountStateNode must feed last_engine_write"
    # Must key off snapshot_timestamp (created_timestamp is None on the singleton
    # -> keying off it would be a silent no-op).
    assert "snapshot_timestamp" in q, \
        "AccountStateNode freshness must use snapshot_timestamp"
    m = re.search(r"AccountStateNode\)(.*?)RETURN(.*?)ts", q, re.S)
    assert m and "snapshot_timestamp" in m.group(2), \
        "AccountStateNode's RETURN must read snapshot_timestamp"
    assert "acc.created_timestamp" not in q, \
        "AccountStateNode must NOT key off created_timestamp (silent no-op trap)"


def test_layer4anomalynode_feeds_heartbeat_via_created_timestamp():
    q = _heartbeat()
    assert "Layer4AnomalyNode" in q, "Layer4AnomalyNode must feed last_engine_write"
    m = re.search(r"Layer4AnomalyNode\)(.*?)RETURN(.*?)ts", q, re.S)
    assert m and "created_timestamp" in m.group(2), \
        "Layer4AnomalyNode freshness must use created_timestamp"


def test_all_eight_node_types_present():
    q = _heartbeat()
    for label in ("TradeNode", "SystemEventNode", "EquitySnapshotNode",
                  "Layer1AnomalyNode", "TradingRuleNode", "PredictionNode",
                  "AccountStateNode", "Layer4AnomalyNode"):
        assert label in q, f"{label} missing from engine_heartbeat"


def test_branch_isolation_on_every_matched_label():
    q = _heartbeat()
    # Every MATCH (x:Label) must be followed by a KCC/KTM exclusion before RETURN.
    matches = re.findall(r"MATCH \((\w+):\w+\)(.*?)RETURN", q, re.S)
    assert matches, "expected MATCH clauses in engine_heartbeat"
    for var, body in matches:
        assert f"NOT {var}:KCCNode" in body and f"NOT {var}:KTMNode" in body, \
            f"branch isolation missing for MATCH var '{var}'"


def test_single_max_aggregate_output():
    q = _heartbeat()
    assert "max(ts) AS last_engine_write" in q, \
        "heartbeat must return a single max(ts) AS last_engine_write"
