"""
Cypher whitelist for the SignalDelta portal proxy.

Portal callers POST {"name": "<query_name>", "params": {...}} to /query. The
proxy looks up the Cypher string here by name. Portal can never inject
arbitrary Cypher — only the pre-authored named queries below are callable.

Per Brian's "proxy is portal-only" rule, this whitelist holds queries the
PORTAL needs to render. Engine-side audits go direct via the neo4j driver.

Sources:
  - Original reconciliation §Panel-by-Panel + §Consolidated 60-Second Poll
  - Reconciliation v1.1+ Section D (D1 Equity Curve, D2 Returns Matrix, D3 Rules)
  - Portal v1.1 dispatch 2026-05-26 (Changes 1-3): pre-market cutoff filter,
    trade list, news ticker

Cutoff filter (portal v1.1 Change 1):
  PORTAL_TRADE_CUTOFF_ISO is auto-injected by main.run_query() into the $cutoff
  param of any query whose name appears in CUTOFF_QUERIES. Queries use
  `datetime(t.entry_timestamp) >= datetime($cutoff)` (cast both sides to
  DateTime to avoid the Z-vs-+00:00 lexicographic-string-compare trap).
  EquitySnapshotNode filters extract the date portion via
  `date(substring($cutoff, 0, 10))`.
"""

# Session 42 ExclusionNode corpus clean (2026-05-29): cutoff advanced to the
# T3.4 clean-baseline fire-up timestamp. All pre-baseline TradeNodes are
# tagged EXCLUDED_FROM_CORPUS in the graph; advancing the portal cutoff here
# ensures the same boundary applies at the proxy boundary. Old test/gate/
# forensic/qty-contaminated trades and stale pre-baseline EquitySnapshotNode
# P&L figures all disappear at the source. Graph nodes are NOT deleted —
# §6.6 immutability holds; they remain as historical/forensic record.
PORTAL_TRADE_CUTOFF_ISO = "2026-05-29T11:29:51Z"


# ── Session 40 forensic exclusion (2026-05-29) ───────────────────────────
# The two δ-silenced TradeNodes from forensic account PA3TLVP8HK4W must not
# contaminate portal analytics (win rate, trade count, conviction tiers,
# returns matrix, trade list). main.run_query() auto-injects the effective
# list as $forensic_ids into any query named in FORENSIC_QUERIES. Stored as
# a constant here so the list is amendable in one place if more forensic IDs
# surface later.
FORENSIC_EXCLUSION_IDS = ["TS-20260526-0003", "TS-20260526-0008", "TS-20260529-0001", "TS-20260529-0002", "TS-20260529-0003"]

# T3-GATE (Session 40, undecided): toggle to also exclude V14 gate trades.
# Default false. The V14 gate-trade identification rule is TBD by the
# operator, so V14_GATE_TRADE_IDS is empty for now — flipping the toggle is
# currently a no-op (it appends an empty list). When the operator defines
# the V14 identification rule, populate V14_GATE_TRADE_IDS and the toggle
# becomes live without any query rewrite.
PORTAL_EXCLUDE_V14_GATE_TRADES = False
V14_GATE_TRADE_IDS: list[str] = []


def effective_forensic_ids() -> list[str]:
    """The forensic exclusion list main.py injects as $forensic_ids.

    Base forensic IDs always; V14 gate trades only when the toggle is on
    (currently a no-op since V14_GATE_TRADE_IDS is empty)."""
    ids = list(FORENSIC_EXCLUSION_IDS)
    if PORTAL_EXCLUDE_V14_GATE_TRADES:
        ids.extend(V14_GATE_TRADE_IDS)
    return ids


# ── §6.6 corrupt-close exclude list (2026-06-10) ─────────────────────────
# The 36 legacy trigger-copy closes from the STEP-0 re-read
# (docs/step0_reread_postfix_recon_20260610T172858Z.md, the 36-trade_id code
# block). §6.6 operator ruling = EXCLUDE-FLAGGED (do NOT correct/rewrite the
# nodes — immutability holds). This is a FROZEN, hand-materialized list, NOT a
# live trigger-copy predicate: a live predicate would over-exclude future
# CORRECT closes that legitimately fill at a trigger level. main.run_query()
# injects this as $corrupt_ids into the all-time $-panel queries
# (CORRUPT_EXCLUDE_QUERIES). Exclusion is a query-time membership filter only.
# Provenance: STEP-0 2026-06-10, §6.6 exclude ruling. Contains GOOGL
# TS-20260608-0014 + TS-20260608-0015.
CORRUPT_EXCLUDE_TRADE_IDS = [
    "TS-20260529-0010", "TS-20260529-0011",
    "TS-20260601-0002", "TS-20260601-0003",
    "TS-20260604-0003", "TS-20260604-0004", "TS-20260604-0010",
    "TS-20260604-0012", "TS-20260604-0031", "TS-20260604-0033",
    "TS-20260604-0049", "TS-20260604-0051", "TS-20260604-0052",
    "TS-20260605-0002", "TS-20260605-0028", "TS-20260605-0034",
    "TS-20260605-0038", "TS-20260605-0043",
    "TS-20260608-0002", "TS-20260608-0004", "TS-20260608-0005",
    "TS-20260608-0014", "TS-20260608-0015", "TS-20260608-0024",
    "TS-20260608-0025", "TS-20260608-0026", "TS-20260608-0028",
    "TS-20260608-0034", "TS-20260608-0037", "TS-20260608-0040",
    "TS-20260608-0042",
    "TS-20260609-0002", "TS-20260609-0003", "TS-20260609-0004",
    "TS-20260609-0005", "TS-20260609-0006",
]


def effective_corrupt_exclude_ids() -> list[str]:
    """The §6.6 exclude list main.py injects as $corrupt_ids into the
    all-time $-panel queries. Frozen constant — sourced from STEP-0, never
    regenerated from a live predicate."""
    return list(CORRUPT_EXCLUDE_TRADE_IDS)


QUERIES = {
    # ── Account bar (Change 1: t_all + t_open both cutoff-filtered) ───────
    # Session 42 (2026-05-29): added EXCLUDED_FROM_CORPUS filter to both t_all and
    # t_open so chaos-era orphan TradeNodes (e.g. TS-20260529-0001/0002) tagged with
    # EXCLUDED_FROM_CORPUS are dropped from the portal OPEN count and trade_count.
    # This resolves the RECON DIFF: 4 graph vs 3 broker banner by bringing open_count
    # in line with the broker position count.
    "account_bar": """
        MATCH (c:TradingConfigNode)
        WITH c.paper_starting_capital AS capital_base, c.current_phase AS current_phase
        OPTIONAL MATCH (e:EquitySnapshotNode)
        WHERE date(e.snapshot_date) >= date(substring($cutoff, 0, 10))
        WITH capital_base, current_phase, e ORDER BY e.snapshot_date DESC LIMIT 1
        OPTIONAL MATCH (t_all:TradeNode)
        WHERE datetime(t_all.entry_timestamp) >= datetime($cutoff)
          AND NOT t_all.trade_id IN $forensic_ids
          AND NOT (t_all)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        WITH capital_base, current_phase, e, count(t_all) AS trade_count
        OPTIONAL MATCH (t_open:TradeNode {status: 'OPEN'})
        WHERE datetime(t_open.entry_timestamp) >= datetime($cutoff)
          AND NOT t_open.trade_id IN $forensic_ids
          AND NOT (t_open)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        RETURN capital_base,
               current_phase,
               e.equity_total AS current_value,
               e.dollar_pnl_today AS today_pnl,
               e.percent_pnl_today AS today_pnl_pct,
               e.sync_timestamp AS last_sync,
               trade_count,
               count(t_open) AS open_count
    """,

    # ── Session 40 rebuild: latest EquitySnapshotNode regardless of cutoff.
    # Denominator for the broker-sourced Today P&L: equity_total of the most
    # recent nightly snapshot (the day's opening baseline). NO cutoff filter —
    # we want the actual last snapshot even if it predates the portal cutoff,
    # because it's the baseline the broker equity is measured against. ───────
    "equity_snapshot_latest": """
        MATCH (e:EquitySnapshotNode)
        WHERE NOT e:KCCNode AND NOT e:KTMNode
        RETURN e.snapshot_date AS snapshot_date,
               e.equity_total AS equity_total,
               e.sync_timestamp AS sync_timestamp
        ORDER BY e.snapshot_date DESC
        LIMIT 1
    """,

    # ── Portal v1.14 P1.3 (2026-05-30): M4 §6 health strip + detail view ──
    # Reads AccountStateNode (M4 §2 schema). Engine M4 Task 6 writes the
    # node; portal builds ahead. One row per account_id. Empty result =
    # node not yet written (Phase 1 strip renders AWAITING ACCOUNT STATE).
    # Branch-isolated per project convention. NOT in CUTOFF/FORENSIC —
    # this is engine-aggregated current state, no TradeNode join.
    "account_state": """
        MATCH (a:AccountStateNode)
        WHERE NOT a:KCCNode AND NOT a:KTMNode
        RETURN a.account_id AS account_id,
               a.snapshot_timestamp AS snapshot_timestamp,
               a.updated_at AS updated_at,
               a.portfolio_value AS portfolio_value,
               a.cash AS cash,
               a.buying_power AS buying_power,
               a.non_marginable_buying_power AS non_marginable_buying_power,
               a.trading_blocked AS trading_blocked,
               a.pattern_day_trader AS pattern_day_trader,
               a.daytrade_count AS daytrade_count,
               a.committed_notional AS committed_notional,
               a.open_position_count AS open_position_count,
               a.headroom_pct AS headroom_pct,
               a.non_marginable_headroom_pct AS non_marginable_headroom_pct,
               a.monitor_coverage_total AS monitor_coverage_total,
               a.monitor_coverage_monitored AS monitor_coverage_monitored,
               a.monitor_coverage_unmonitored AS monitor_coverage_unmonitored,
               a.monitor_coverage_unmonitored_trade_ids AS monitor_coverage_unmonitored_trade_ids,
               a.monitor_mismatch_count_last_cycle AS monitor_mismatch_count_last_cycle,
               a.health_state AS health_state,
               a.health_reasons AS health_reasons
        ORDER BY a.account_id ASC
    """,

    # ── Portal v1.14 P1.4 (2026-05-30): 24h health-anomaly history for the
    # M4 detail-view history block. Reads Layer4AnomalyNode where
    # anomaly_type is one of the two account-health events; portal parses
    # the JSON `details` blob to filter by account_id at render time.
    # Branch-isolated; NOT in CUTOFF/FORENSIC.
    "account_health_history": """
        MATCH (n:Layer4AnomalyNode)
        WHERE NOT n:KCCNode AND NOT n:KTMNode
          AND n.anomaly_type IN ['ACCOUNT_HEALTH_DEGRADED', 'ACCOUNT_HEALTH_RECOVERED']
          AND datetime(n.created_timestamp) >= datetime() - duration({hours: 24})
        RETURN n.anomaly_type AS anomaly_type,
               n.severity AS severity,
               n.created_timestamp AS created_timestamp,
               n.details AS details
        ORDER BY n.created_timestamp DESC
        LIMIT 200
    """,

    # Portal v1.14 P1.1 (2026-05-30): LIMIT 6 -> 13. M4 top-stack redesign
    # rolls the week tracker to 5-slot default with a rolling-window MAX 13
    # (one quarter), 14th rolls the oldest off. Without this bump the
    # rolling display would silently truncate at 6.
    "weekly_waterfall": """
        MATCH (w:WeeklyContextNode)
        RETURN w.week_start_date AS week_start,
               w.system_weekly_pnl_pct AS pnl_pct
        ORDER BY w.week_start_date DESC
        LIMIT 13
    """,

    # Open-positions query retained for any caller still referencing it.
    # Portal v1.1 Change 2 switches the panel to `trade_list_recent` (below).
    # Session 42: added EXCLUDED_FROM_CORPUS filter (chaos-era orphan exclusion).
    "open_positions": """
        MATCH (t:TradeNode {status: 'OPEN'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        RETURN t.request_id AS request_id,
               t.asset AS asset,
               t.track AS track,
               t.conviction_tier AS conviction,
               t.entry_price AS entry_price,
               t.stop_loss_price AS stop_price,
               t.current_stop AS current_stop,
               t.target_price AS target_price,
               t.direction AS direction,
               t.entry_timestamp AS entry_timestamp
        ORDER BY t.entry_timestamp DESC
        LIMIT 3
    """,

    # ── Portal v1.1 Change 2: trade list (both OPEN and CLOSED) ───────────
    # Session 42: added EXCLUDED_FROM_CORPUS filter (chaos-era orphan exclusion).
    # Portal v1.10 (2026-05-29): LIMIT 12 → 50. Center-column layout fix
    # grows the trades panel to ~15-row capacity; LIMIT 12 would have
    # silently capped the table below that. ORDER BY entry_timestamp DESC
    # + panel clip keeps the most-recent rows visible (older clipped),
    # same model as before — this is future-proof headroom, not a visible
    # change today (current corpus = 8).
    # Portal recon-diff netting (2026-06-06): added t.position_size so the
    # frontend adaptReconciliation can sum position_size per (asset, direction)
    # group and compare against the broker's consolidated qty, mirroring the
    # engine's S50 netting. Without it the portal did a raw count comparison
    # (2 graph legs vs 1 broker position) and falsely flagged a reconciled
    # multi-leg state as RECON DIFF.
    "trade_list_recent": """
        MATCH (t:TradeNode)
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        RETURN t.trade_id AS trade_id,
               t.request_id AS request_id,
               t.asset AS asset,
               t.track AS track,
               t.conviction_tier AS conviction,
               t.entry_price AS entry_price,
               t.exit_price AS exit_price,
               t.stop_loss_price AS stop_price,
               t.current_stop AS current_stop,
               t.target_price AS target_price,
               t.direction AS direction,
               t.entry_timestamp AS entry_timestamp,
               t.exit_timestamp AS exit_timestamp,
               t.status AS status,
               t.pnl_dollar AS pnl_dollar,
               t.pnl_percent AS pnl_percent,
               t.realized_pnl AS realized_pnl,
               t.win_loss AS win_loss,
               t.hold_duration_min AS hold_duration_min,
               t.exit_reason AS exit_reason,
               t.position_size AS position_size
        ORDER BY t.entry_timestamp DESC
        LIMIT 50
    """,
    # ── Portal Rev 32 (2026-06-05): windowed trade list for the EXPAND modal.
    # Mirrors trade_list_recent EXACTLY — branch isolation, $cutoff, $forensic_ids,
    # EXCLUDED_FROM_CORPUS, and the full RETURN column list — so the existing
    # adaptTradeList() maps it unchanged. Adds a client-driven lower bound
    # $window_start (ISO-8601 UTC). The modal "all" preset passes 1970-01-01 so
    # the existing $cutoff governs the floor (modal scope == panel scope).
    # LIMIT 2000 is a safety ceiling, not a row cap the operator will hit.
    "trade_list_window": """
        MATCH (t:TradeNode)
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND datetime(t.entry_timestamp) >= datetime($cutoff)
          AND datetime(t.entry_timestamp) >= datetime($window_start)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        RETURN t.trade_id AS trade_id,
               t.request_id AS request_id,
               t.asset AS asset,
               t.track AS track,
               t.conviction_tier AS conviction,
               t.entry_price AS entry_price,
               t.exit_price AS exit_price,
               t.stop_loss_price AS stop_price,
               t.current_stop AS current_stop,
               t.target_price AS target_price,
               t.direction AS direction,
               t.entry_timestamp AS entry_timestamp,
               t.exit_timestamp AS exit_timestamp,
               t.status AS status,
               t.pnl_dollar AS pnl_dollar,
               t.pnl_percent AS pnl_percent,
               t.realized_pnl AS realized_pnl,
               t.win_loss AS win_loss,
               t.hold_duration_min AS hold_duration_min,
               t.exit_reason AS exit_reason,
               t.position_size AS position_size
        ORDER BY t.entry_timestamp DESC
        LIMIT 2000
    """,
    # ── Portal Rev 42 (2026-06-04): single-ET-day CLOSED feed for the DAY W/L
    # (ET) banner stat + the ALL TRADES modal day buckets. Mirrors
    # trade_list_window's isolation + FULL RETURN columns (so adaptTradeList maps
    # it unchanged) but filters CLOSED trades by EXIT timestamp within the
    # client-supplied [day_start, day_end) ET-calendar-day instants. $cutoff +
    # $forensic_ids auto-inject (same scope as the panel). LIMIT 2000 ceiling.
    "trades_closed_day": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND datetime(t.entry_timestamp) >= datetime($cutoff)
          AND datetime(t.exit_timestamp) >= datetime($day_start)
          AND datetime(t.exit_timestamp) <  datetime($day_end)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        RETURN t.trade_id AS trade_id,
               t.request_id AS request_id,
               t.asset AS asset,
               t.track AS track,
               t.conviction_tier AS conviction,
               t.entry_price AS entry_price,
               t.exit_price AS exit_price,
               t.stop_loss_price AS stop_price,
               t.current_stop AS current_stop,
               t.target_price AS target_price,
               t.direction AS direction,
               t.entry_timestamp AS entry_timestamp,
               t.exit_timestamp AS exit_timestamp,
               t.status AS status,
               t.pnl_dollar AS pnl_dollar,
               t.pnl_percent AS pnl_percent,
               t.realized_pnl AS realized_pnl,
               t.win_loss AS win_loss,
               t.hold_duration_min AS hold_duration_min,
               t.exit_reason AS exit_reason,
               t.position_size AS position_size
        ORDER BY t.exit_timestamp DESC
        LIMIT 2000
    """,

    # Portal v1.1 dispatch 2026-05-26 (status-strip 5-event cycle): dropped
    # the 30-min lookback so the strip always shows the 5 most recent events
    # regardless of age. KCC/KTM isolation added for branch consistency.
    # LIMIT stays at 12 — portal slices [0..5] for the cycle; the extra rows
    # are a cheap headroom for any future consumer wanting more depth.
    "recent_events": """
        MATCH (e:SystemEventNode)
        WHERE NOT e:KCCNode AND NOT e:KTMNode
        RETURN e.event_id AS event_id,
               e.event_type AS event_type,
               e.event_subtype AS event_subtype,
               e.asset AS asset,
               e.timestamp AS event_timestamp,
               e.summary AS summary,
               e.severity AS severity
        ORDER BY e.timestamp DESC
        LIMIT 12
    """,

    # ── Win rate (Change 1: cutoff applied to CLOSED trades) ──────────────
    "win_rate": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        RETURN count(t) AS total_closed,
               sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
               avg(CASE WHEN t.win_loss = 'Win' THEN 1.0 ELSE 0.0 END) * 100 AS win_rate_pct
    """,

    "sharpe_ratio": """
        MATCH (w:WeeklyContextNode)
        RETURN w.sharpe_ratio_combined AS sr,
               w.sharpe_ratio_crypto AS sr_crypto,
               w.sharpe_ratio_stocks AS sr_stocks,
               w.total_trades_week AS week_trades,
               w.week_start_date AS as_of
        ORDER BY w.week_start_date DESC
        LIMIT 1
    """,

    # ── Lane 2 Δ (Change 1: cutoff applied to CLOSED trades; PredictionNode
    # filter is by status only, unchanged) ────────────────────────────────
    "lane2_delta": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        WITH count(t) AS closed_count,
             avg(CASE WHEN t.win_loss = 'Win' THEN 1.0 ELSE 0.0 END) AS l1_rate
        OPTIONAL MATCH (p:PredictionNode)
        WHERE p.status IN ['CONFIRMED', 'INVALIDATED']
        WITH closed_count, l1_rate,
             count(p) AS resolved_count,
             avg(CASE WHEN p.status = 'CONFIRMED' THEN 1.0 ELSE 0.0 END) AS l2_rate
        MATCH (c:TradingConfigNode)
        RETURN closed_count,
               l1_rate * 100 AS l1_win_rate_pct,
               resolved_count,
               l2_rate * 100 AS l2_confirm_rate_pct,
               (l2_rate - l1_rate) * 100 AS delta_pct,
               c.lane2_enabled AS lane2_enabled
    """,

    # ── Conviction tiers (Change 1: cutoff applied to all TradeNodes) ─────
    "conviction_tiers": """
        MATCH (t:TradeNode)
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        WITH count(t) AS total
        MATCH (t:TradeNode)
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        WITH total, t.conviction_tier AS tier, count(t) AS tier_count
        RETURN tier, tier_count, (tier_count * 1.0 / total) * 100 AS tier_pct
    """,

    "kernel_nodes": """
        MATCH (i:IndicatorNode)
        RETURN i.node_id AS node_id,
               i.cluster AS cluster,
               i.confirmation_rate AS confirmation_rate,
               i.prediction_count AS prediction_count,
               i.weight AS weight,
               i.last_active AS last_active,
               i.status AS status,
               i.added_cycle AS added_cycle
    """,

    "kernel_edges": """
        MATCH (i1:IndicatorNode)-[r:CO_OCCURS_WITH]->(i2:IndicatorNode)
        RETURN i1.node_id AS source_id,
               i2.node_id AS target_id,
               r.strength AS edge_opacity_source,
               r.count AS edge_count
    """,

    # ── Equity curve (Change 1: cutoff applied — engine launch date) ──────
    "equity_curve_series": """
        MATCH (e:EquitySnapshotNode)
        WHERE date(e.snapshot_date) >= date(substring($cutoff, 0, 10))
        RETURN e.snapshot_date AS snapshot_date,
               e.equity_total AS equity
        ORDER BY e.snapshot_date ASC
    """,

    "equity_curve_stats": """
        MATCH (e:EquitySnapshotNode)
        WHERE date(e.snapshot_date) >= date(substring($cutoff, 0, 10))
        RETURN e.peak_equity_to_date AS peak,
               e.max_drawdown_to_date_percent AS drawdown_pct,
               e.twr_to_date_percent AS twr_pct,
               e.snapshot_date AS as_of
        ORDER BY e.snapshot_date DESC
        LIMIT 1
    """,

    # ── Returns matrix (Change 1: cutoff applied to every cell + sigma) ───
    "returns_matrix_cell": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
          AND (CASE t.asset_class WHEN 'Large Cap Stock' THEN 'Large-cap stock' ELSE t.asset_class END) = $asset_class
          AND t.track = $track
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    "returns_matrix_sigma_row": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
          AND t.track = $track
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    "returns_matrix_sigma_col": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
          AND (CASE t.asset_class WHEN 'Large Cap Stock' THEN 'Large-cap stock' ELSE t.asset_class END) = $asset_class
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    "returns_matrix_sigma_corner": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND NOT t.trade_id IN $forensic_ids
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    "rules_this_week": """
        MATCH (r:TradingRuleNode)
        WHERE r.created_timestamp >= date.truncate('week', date())
        RETURN r.rule_id AS rule_id,
               r.section AS section,
               r.created_timestamp AS created,
               r.cycle_number AS cycle,
               r.summary AS summary
        ORDER BY r.created_timestamp DESC
        LIMIT 5
    """,

    "rules_footer": """
        MATCH (r:TradingRuleNode)
        WITH count(r) AS total,
             max(r.cycle_number) AS latest_cycle
        OPTIONAL MATCH (r2:TradingRuleNode)
        WHERE r2.created_timestamp >= date.truncate('week', date())
        RETURN total, latest_cycle, count(r2) AS this_week_count
    """,

    # Closest single-dimension cohort to the rule-action floor (2026-06-08).
    # Powers the "RULES ADDED" empty-state progress ("Largest cohort N / floor").
    # Mirrors the §11 learning-loop corpus filters exactly (branch-isolated,
    # non-adopted, not EXCLUDED_FROM_CORPUS, not Manual override) so it reflects
    # what the loop sees when deciding rule eligibility. Placeholder/null/UNKNOWN
    # dimension values are excluded so a degenerate cohort can't mask the real
    # closest meaningful one. NOT cutoff-filtered: the loop corpus is full, not
    # the portal display window. Cheap (counts only).
    "closest_cohort": """
        MATCH (cfg:TradingConfigNode)
        WHERE NOT cfg:KCCNode AND NOT cfg:KTMNode
        WITH cfg.cohort_min_count_for_action AS rule_floor
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT coalesce(t.adopted_from_broker, false)
          AND NOT (t)-[:EXCLUDED_FROM_CORPUS]->(:ExclusionNode)
          AND t.exit_reason <> 'Manual override'
        UNWIND [
            ['track', t.track],
            ['conviction_tier', t.conviction_tier],
            ['time_bucket', t.time_bucket],
            ['market_condition', t.market_condition],
            ['asset_class', t.asset_class],
            ['news_event', t.news_event],
            ['direction', t.direction]
        ] AS dv
        WITH rule_floor, dv[0] AS dim, toString(dv[1]) AS val
        WHERE dv[1] IS NOT NULL AND toString(dv[1]) <> '' AND toString(dv[1]) <> 'UNKNOWN'
        WITH rule_floor, dim, val, count(*) AS cnt
        ORDER BY cnt DESC, dim ASC, val ASC
        LIMIT 1
        RETURN dim AS closest_cohort_dimension,
               val AS closest_cohort_value,
               cnt AS closest_cohort_count,
               rule_floor AS rule_floor
    """,

    "monitored_assets": """
        MATCH (c:TradingConfigNode)
        RETURN c.monitored_assets AS asset_list
    """,

    "trade_overlay_enrichment": """
        MATCH (t:TradeNode {request_id: $request_id})
        OPTIONAL MATCH (t)-[:HAS_PREDICTION]->(p:PredictionNode)
        RETURN t.asset AS asset,
               t.track AS track,
               t.conviction_tier AS conviction,
               t.entry_price AS entry_price,
               t.exit_price AS exit_price,
               t.stop_loss_price AS stop_price,
               t.current_stop AS current_stop,
               t.target_price AS target_price,
               t.composite_score AS composite_score,
               t.lane2_score AS lane2_score,
               t.rsi_at_entry AS rsi,
               t.ema_signal AS ema_signal,
               t.vwap_position AS vwap_position,
               t.macd_signal AS macd_signal,
               t.pnl_dollar AS pnl_dollar,
               t.pnl_percent AS pnl_percent,
               t.exit_reason AS exit_reason,
               t.win_loss AS win_loss,
               t.hold_duration_min AS hold_duration_min,
               t.status AS status,
               p.lane2_confidence AS lane2_confidence,
               p.status AS prediction_status
    """,

    # ── Diagnostics — 2026-05-26 equity drift audit, kept long-term ──────
    "diag_trade_counts": """
        MATCH (t:TradeNode)
        WHERE NOT t:KCCNode
        RETURN count(t) AS total_trades,
               sum(CASE WHEN t.status = 'OPEN' THEN 1 ELSE 0 END) AS open_count,
               sum(CASE WHEN t.status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_count
    """,

    "diag_realized_pnl": """
        MATCH (t:TradeNode)
        WHERE NOT t:KCCNode AND t.status = 'CLOSED'
        RETURN sum(coalesce(t.realized_pnl, 0)) AS total_realized_pnl,
               collect(t.realized_pnl)[..20] AS pnl_sample
    """,

    "diag_equity_snapshots": """
        MATCH (e:EquitySnapshotNode)
        WHERE NOT e:KCCNode
        RETURN e.snapshot_date AS snapshot_date,
               e.equity AS equity,
               e.realized_pnl AS realized_pnl,
               e.unrealized_pnl AS unrealized_pnl
        ORDER BY e.snapshot_date DESC
        LIMIT 5
    """,

    "diag_capital_flows": """
        MATCH (c:CapitalFlowNode)
        WHERE NOT c:KCCNode
        RETURN c.flow_type AS flow_type,
               c.amount AS amount,
               c.timestamp AS timestamp
        ORDER BY c.timestamp DESC
        LIMIT 10
    """,

    "diag_equity_nodes": """
        MATCH (a)
        WHERE a.account_equity IS NOT NULL OR a.equity IS NOT NULL
        RETURN labels(a) AS node_labels,
               a.account_equity AS account_equity,
               a.equity AS equity,
               coalesce(a.timestamp, a.created_timestamp, a.snapshot_date) AS ts
        ORDER BY ts DESC
        LIMIT 10
    """,

    # ── Engine heartbeat ─────────────────────────────────────────────────
    "engine_heartbeat": """
        CALL {
          MATCH (t:TradeNode)
          WHERE NOT t:KCCNode AND NOT t:KTMNode
          RETURN max(coalesce(t.exit_timestamp, t.entry_timestamp)) AS ts
          UNION ALL
          MATCH (e:SystemEventNode)
          WHERE NOT e:KCCNode AND NOT e:KTMNode
          RETURN max(e.timestamp) AS ts
          UNION ALL
          MATCH (es:EquitySnapshotNode)
          WHERE NOT es:KCCNode AND NOT es:KTMNode
          RETURN max(coalesce(es.sync_timestamp, es.created_timestamp)) AS ts
          UNION ALL
          MATCH (a:Layer1AnomalyNode)
          WHERE NOT a:KCCNode AND NOT a:KTMNode
          RETURN max(coalesce(a.created_timestamp, a.timestamp)) AS ts
          UNION ALL
          MATCH (r:TradingRuleNode)
          WHERE NOT r:KCCNode AND NOT r:KTMNode
          RETURN max(r.created_timestamp) AS ts
          UNION ALL
          MATCH (p:PredictionNode)
          WHERE NOT p:KCCNode AND NOT p:KTMNode
          RETURN max(coalesce(p.created_timestamp, p.prediction_timestamp)) AS ts
          UNION ALL
          // 2026-06-06 heartbeat fix: AccountStateNode is the ~30s steady
          // signal the engine writes even on a closed-market day. Freshness
          // lives in snapshot_timestamp (created_timestamp is None on this
          // MERGE'd singleton — keying off it would be a silent no-op).
          MATCH (acc:AccountStateNode)
          WHERE NOT acc:KCCNode AND NOT acc:KTMNode
          RETURN max(coalesce(acc.snapshot_timestamp, acc.updated_at)) AS ts
          UNION ALL
          // Layer4AnomalyNode = ~15min reconcile cycle; freshness in created_timestamp.
          MATCH (l4:Layer4AnomalyNode)
          WHERE NOT l4:KCCNode AND NOT l4:KTMNode
          RETURN max(l4.created_timestamp) AS ts
        }
        RETURN max(ts) AS last_engine_write
    """,

    # ── Portal v1.2 scanner-cycle dispatch (2026-05-26): most-recent
    # composite_score per monitored asset, cutoff-filtered. Caller supplies
    # $asset_list (from a mount-time monitored_assets read). Assets in the
    # list with no qualifying event return zero rows — the portal renders
    # those as "BUILDING DATA" by diffing against the cached asset list.
    #
    # SCANNER FILTER FIX (2026-05-27): switched source node from TradeNode
    # to SystemEventNode {event_type:'THRESHOLD_HIT'}. The TradeNode-based
    # version excluded 18 of 23 monitored assets because Layer 6 rejects
    # most of them for MAX_POSITION_CAP_EXCEEDED on the $10K Phase 1
    # account ($100+ stocks > 50% cap once a few positions are open). The
    # signal engine IS scoring all 23 — proven by THRESHOLD_HIT events
    # firing for every monitored asset in-window. THRESHOLD_HIT is the
    # right source: it fires the moment a composite_score clears any one
    # of the three track thresholds, BEFORE the L6 sizing gate that drops
    # most assets from the TradeNode chain.
    #
    # composite_score lives inside the summary string, not as a top-level
    # property. Format is stable: "<ASSET> composite_score=NN.NN cleared
    # <tracks>" — split on the literal sentinel to extract the float. The
    # long-term cleanup is an engine-side §14 amendment adding
    # composite_score as a top-level SystemEventNode property; deferred to
    # a separate dispatch. last_track also dropped from the return — the
    # portal's adapter falls back to its existing CRY/STK asset-class
    # sublabel for non-FIRED rows. ─────────────────────────────────────────
    "scanner_scores": """
        MATCH (e:SystemEventNode)
        WHERE NOT e:KCCNode AND NOT e:KTMNode
          AND e.event_type = 'THRESHOLD_HIT'
          AND e.asset IN $asset_list
          AND datetime(e.timestamp) >= datetime($cutoff)
        WITH e.asset AS asset, e ORDER BY e.timestamp DESC
        WITH asset, head(collect(e)) AS most_recent
        RETURN asset,
               toFloat(split(split(most_recent.summary, 'composite_score=')[1], ' ')[0]) AS last_score,
               most_recent.timestamp AS last_seen
    """,

    # ── Scanner Tier 2 (2026-06-09): LIVE per-bar gate state ──────────────────
    # Reads the engine's rolling ScannerLiveStateNode (Layer 2 emits/overwrites
    # one node per asset every bar — Tier 2). Replaces scanner_scores as the
    # scanner source: live composite (not last-cleared) + the raw gate inputs.
    # main.run_query() enriches each row server-side with the GO decision
    # (G1∧G2∧G3∧tradable∧fresh) in _enrich_scanner_live_state() — the proxy has
    # the ET clock + threshold config the engine doesn't expose. No cutoff
    # (live state, not history).
    "scanner_live_state": """
        MATCH (s:ScannerLiveStateNode)
        WHERE NOT s:KCCNode AND NOT s:KTMNode
        RETURN s.asset AS asset, s.composite_score AS composite,
               s.contributors_count AS contributors, s.g2_agreed AS g2_agreed,
               s.direction AS direction, s.evaluation_timestamp AS eval_ts,
               s.data_fresh AS data_fresh
    """,

    # ── Portal v1.1 Change 3A: News ticker — non-QUIET NewsContextNodes ──
    "news_ticker_recent": """
        MATCH (n:NewsContextNode)
        WHERE NOT n:KCCNode AND NOT n:KTMNode
          AND n.event_type <> 'QUIET'
        RETURN n.asset AS asset,
               n.event_type AS event_type,
               n.impact_level AS impact_level,
               n.event_summary AS event_summary,
               n.source AS source,
               n.written_at AS written_at,
               n.date AS scan_date
        ORDER BY n.written_at DESC
        LIMIT 50
    """,

    # ══════════════════════════════════════════════════════════════════════
    # §6.6 all-time $-panels (2026-06-10). Exclude the 36 frozen corrupt
    # trigger-copy closes ($corrupt_ids, auto-injected). Filter set matches the
    # operator-ruled STEP-0 (c) baseline EXACTLY: branch-iso + status=CLOSED +
    # excl-corrupt ONLY — deliberately NOT layered with the portal cutoff /
    # forensic / ExclusionNode filters (those would move the totals off the
    # §6.6-ruled numbers). asset_class is normalized to canonical
    # 'Large-cap stock' wherever it is a DISPLAY dimension (Returns-by-Domain)
    # so adopted-from-broker 'Large Cap Stock' nodes fold into the canonical
    # cell (Item 94 normalization, same CASE as returns_matrix_*). by-track /
    # profit-factor / expectancy do not group on asset_class → normalization is
    # a no-op there. The Sharpe panel deliberately uses RAW asset_class (see
    # its note) — normalizing would alter the §12 risk-free weighting and break
    # exact reproduction of the operator-ruled value.
    "panel_pnl_by_track": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
        RETURN t.track AS track,
               count(t) AS n,
               sum(t.pnl_dollar) AS pnl_dollar
        ORDER BY track
    """,

    "panel_returns_by_domain": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
        WITH t.track AS track,
             (CASE t.asset_class WHEN 'Large Cap Stock' THEN 'Large-cap stock'
                   ELSE t.asset_class END) AS asset_class,
             t.pnl_dollar AS pnl
        RETURN track, asset_class,
               count(*) AS n,
               sum(pnl) AS pnl_dollar
        ORDER BY track, asset_class
    """,

    "panel_profit_factor": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
        WITH t.track AS scope, t.pnl_dollar AS pnl
        WITH scope, count(*) AS n,
             sum(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
             sum(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END) AS gross_loss
        RETURN scope, n, gross_profit, gross_loss,
               CASE WHEN gross_loss = 0 THEN null ELSE gross_profit / gross_loss END
                   AS profit_factor
        ORDER BY scope
        UNION ALL
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
        WITH t.pnl_dollar AS pnl
        WITH count(*) AS n,
             sum(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
             sum(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END) AS gross_loss
        RETURN '__OVERALL__' AS scope, n, gross_profit, gross_loss,
               CASE WHEN gross_loss = 0 THEN null ELSE gross_profit / gross_loss END
                   AS profit_factor
    """,

    "panel_expectancy": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
        WITH t.track AS scope, t.pnl_dollar AS pnl
        RETURN scope, count(*) AS n,
               avg(pnl) AS expectancy_dollar,
               sum(pnl) AS pnl_dollar
        ORDER BY scope
        UNION ALL
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
        WITH t.pnl_dollar AS pnl
        RETURN '__OVERALL__' AS scope, count(*) AS n,
               avg(pnl) AS expectancy_dollar,
               sum(pnl) AS pnl_dollar
    """,

    # Per-trade log-return Sharpe (basis ruled by operator at STEP-0), excl
    # the 36 corrupt closes. Replicates §12 layer12_sharpe.compute EXACTLY in
    # Cypher: Long ln(exit/entry) / Short ln(entry/exit); mean; sample stddev
    # (n-1 Bessel via reduce); risk_free='auto' (stock-subset weighted, crypto
    # weight contributes 0) using RAW asset_class buckets ['Large-cap stock',
    # 'Growth stock'] — NOT normalized, matching §12 + the STEP-0 input.
    # Band/confidence thresholds read LIVE from TradingConfigNode (same source
    # the engine reads) so there is no hardcoded ladder and no drift. Also
    # serves the parked daily-equity basis honestly: daily_equity_basis_available
    # = false + insufficient_history (distinct EquitySnapshotNode days < 30) so
    # the portal never renders the annualized basis as a usable number.
    "panel_sharpe_excl_corrupt": """
        MATCH (e:EquitySnapshotNode)
        WHERE NOT e:KCCNode AND NOT e:KTMNode
        WITH e ORDER BY e.snapshot_date
        WITH count(DISTINCT e.snapshot_date) AS equity_days,
             [x IN collect(e.equity_total) WHERE x IS NOT NULL | toFloat(x)] AS eq
        WITH equity_days, eq,
             [i IN range(1, size(eq) - 1) WHERE eq[i - 1] > 0.0 | eq[i] / eq[i - 1] - 1.0] AS drets
        WITH equity_days, size(drets) AS dn, drets,
             CASE WHEN size(drets) = 0 THEN 0.0
                  ELSE reduce(s = 0.0, r IN drets | s + r) / size(drets) END AS dmean
        WITH equity_days, dn, drets, dmean,
             CASE WHEN dn > 1
                  THEN sqrt(reduce(s = 0.0, r IN drets | s + (r - dmean) * (r - dmean)) / (dn - 1))
                  ELSE 0.0 END AS dstd
        WITH equity_days, dn,
             CASE WHEN dstd > 0.0 THEN (dmean / dstd) * sqrt(252.0) ELSE 0.0 END AS daily_sharpe_annualized
        MATCH (cfg:TradingConfigNode)
        WHERE NOT cfg:KCCNode AND NOT cfg:KTMNode
        WITH equity_days, dn, daily_sharpe_annualized, cfg LIMIT 1
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
        WITH equity_days, dn, daily_sharpe_annualized, cfg, collect(t) AS trades,
             min(t.entry_timestamp) AS min_entry, max(t.exit_timestamp) AS max_exit
        WITH equity_days, dn, daily_sharpe_annualized, cfg, trades,
             CASE WHEN min_entry IS NULL OR max_exit IS NULL THEN 0.0
                  ELSE (datetime(max_exit).epochSeconds - datetime(min_entry).epochSeconds) / 86400.0
             END AS span_days
        WITH equity_days, dn, daily_sharpe_annualized, cfg, span_days, size(trades) AS n,
             [x IN trades | CASE WHEN x.direction = 'Long'
                  THEN log(toFloat(x.exit_price) / toFloat(x.entry_price))
                  ELSE log(toFloat(x.entry_price) / toFloat(x.exit_price)) END] AS logrets,
             [x IN trades WHERE x.asset_class IN ['Large-cap stock', 'Growth stock']
                  | toFloat(x.hold_duration_min) / 1440.0] AS stock_holds
        WITH equity_days, dn, daily_sharpe_annualized, cfg, span_days, n, logrets, stock_holds,
             reduce(s = 0.0, r IN logrets | s + r) / n AS mean_lr
        WITH equity_days, dn, daily_sharpe_annualized, cfg, span_days, n, mean_lr,
             sqrt(reduce(s = 0.0, r IN logrets | s + (r - mean_lr) * (r - mean_lr)) / (n - 1))
                 AS std_lr,
             CASE WHEN size(stock_holds) = 0 THEN 0.0
                  ELSE reduce(s = 0.0, h IN stock_holds | s + h) / size(stock_holds) END
                 AS stocks_avg_hold_days,
             toFloat(size(stock_holds)) / n AS stock_weight
        WITH equity_days, dn, daily_sharpe_annualized, cfg, span_days, n, mean_lr, std_lr,
             stock_weight * ((toFloat(cfg.risk_free_rate_stocks) / 252.0) * stocks_avg_hold_days)
                 AS rf_per_trade
        WITH equity_days, dn, daily_sharpe_annualized, cfg, span_days, n, mean_lr, std_lr,
             CASE WHEN std_lr = 0.0 THEN 0.0
                  ELSE (mean_lr - rf_per_trade) / std_lr END AS sharpe
        // Annualized-corpus Sharpe = per-trade × √(annual trade frequency), where
        // annual trade frequency = (trades / corpus-span-days) × 252 (operator-set
        // 06-09 display basis). The dial reads THIS; daily ×√252 is too noisy on a
        // <30-day daily-return series. Factor served for transparency.
        WITH equity_days, dn, daily_sharpe_annualized, cfg, span_days, n, sharpe,
             CASE WHEN span_days > 0.0 THEN sqrt((toFloat(n) / span_days) * 252.0) ELSE 0.0 END
                 AS corpus_freq_factor
        WITH equity_days, dn, daily_sharpe_annualized, cfg, span_days, n, sharpe, corpus_freq_factor,
             sharpe * corpus_freq_factor AS annualized_corpus_sharpe
        RETURN sharpe AS sharpe_value,
               n AS n,
               'per_trade_log_return_excl_corrupt' AS basis_label,
               CASE WHEN sharpe > toFloat(cfg.sharpe_well_threshold) THEN 'WELL'
                    WHEN sharpe >= toFloat(cfg.sharpe_acceptable_floor) THEN 'ACCEPTABLE'
                    WHEN sharpe >= toFloat(cfg.sharpe_warning_floor) THEN 'WARNING'
                    ELSE 'CRITICAL' END AS band,
               CASE WHEN n < toInteger(cfg.sharpe_low_confidence_count) THEN 'LOW'
                    WHEN n < toInteger(cfg.sharpe_high_confidence_count) THEN 'MEDIUM'
                    ELSE 'HIGH' END AS confidence,
               annualized_corpus_sharpe AS annualized_corpus_sharpe,
               corpus_freq_factor AS corpus_freq_factor,
               span_days AS corpus_span_days,
               CASE WHEN annualized_corpus_sharpe > toFloat(cfg.sharpe_well_threshold) THEN 'WELL'
                    WHEN annualized_corpus_sharpe >= toFloat(cfg.sharpe_acceptable_floor) THEN 'ACCEPTABLE'
                    WHEN annualized_corpus_sharpe >= toFloat(cfg.sharpe_warning_floor) THEN 'WARNING'
                    ELSE 'CRITICAL' END AS annualized_corpus_band,
               daily_sharpe_annualized AS daily_sharpe_value,
               'daily_equity_x_sqrt252' AS daily_basis_label,
               true AS daily_equity_basis_available,
               dn AS daily_return_count,
               equity_days AS equity_days,
               (equity_days < 30) AS insufficient_history
    """,

    # PF + Expectancy daily series (2026-06-10) — for the KPI-tile sparklines.
    # Per exit-day over the excl-36 closed corpus: gross_profit, gross_loss,
    # sum_pnl, n. main.run_query() post-processes (_enrich_pf_expectancy_series)
    # into cumulative-to-date profit_factor + expectancy series for the spark.
    # Branch-iso; $corrupt_ids auto-injected.
    "panel_pf_expectancy_series": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
          AND t.exit_timestamp IS NOT NULL
        WITH substring(t.exit_timestamp, 0, 10) AS day, t.pnl_dollar AS pnl
        RETURN day,
               count(*) AS n,
               sum(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
               sum(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END) AS gross_loss,
               sum(pnl) AS sum_pnl
        ORDER BY day
    """,

    # §6.6 Returns-by-Domain ANNUALIZED % (2026-06-10) — sibling of the $ view
    # (panel_returns_by_domain). Returns RAW per-cell inputs (collected
    # pnl_percent + summed pnl_dollar + n) + equity_days; main.run_query()
    # post-processes (_enrich_returns_by_domain_pct) into cells + Total rim +
    # Total corner, each annualized per the OPERATOR formula
    # (1 + cum)^(252 / equity_days) − 1 with cum = compounded per-trade return.
    # SAME filters as the $ view (exclude-36 via $corrupt_ids, asset_class folded,
    # branch-iso) so $ and % share ONE cell population. $window bounds the cohort
    # by exit_timestamp; equity_days = distinct EquitySnapshotNode days in the
    # same window. window ∈ {current_month, ytd, 1y, 5y, all}; window_start is
    # derived in-query from the server date (all → null → unbounded).
    "panel_returns_by_domain_pct": """
        WITH (CASE $window
                WHEN 'current_month' THEN substring(toString(date.truncate('month', date())), 0, 10)
                WHEN 'ytd'           THEN substring(toString(date.truncate('year', date())), 0, 10)
                WHEN '1y'            THEN substring(toString(date() - duration({years: 1})), 0, 10)
                WHEN '5y'            THEN substring(toString(date() - duration({years: 5})), 0, 10)
                ELSE null END) AS window_start
        CALL {
            WITH window_start
            MATCH (e:EquitySnapshotNode)
            WHERE NOT e:KCCNode AND NOT e:KTMNode
              AND (window_start IS NULL OR e.snapshot_date >= window_start)
            RETURN count(DISTINCT e.snapshot_date) AS equity_days
        }
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND NOT t.trade_id IN $corrupt_ids
          AND t.pnl_percent IS NOT NULL
          AND (window_start IS NULL OR substring(t.exit_timestamp, 0, 10) >= window_start)
        WITH equity_days,
             t.track AS track,
             (CASE t.asset_class WHEN 'Large Cap Stock' THEN 'Large-cap stock'
                   ELSE t.asset_class END) AS asset_class,
             collect(t.pnl_percent) AS returns,
             count(*) AS n,
             sum(t.pnl_dollar) AS pnl_dollar
        RETURN track, asset_class, n, pnl_dollar, returns, equity_days
        ORDER BY track, asset_class
    """,

    # Account-level ANNUALIZED return for the header "Ann" field (2026-06-10).
    # Returns RAW inputs (latest EquitySnapshotNode equity_total vs
    # paper_starting_capital → cum_return; equity_days). main.run_query()
    # post-processes (_enrich_annualized_return) with the SAME operator formula
    # + insufficient_history flag — so the header shows the annualized value WITH
    # the flag instead of "building".
    "panel_annualized_return": """
        MATCH (e:EquitySnapshotNode)
        WHERE NOT e:KCCNode AND NOT e:KTMNode
        WITH count(DISTINCT e.snapshot_date) AS equity_days
        MATCH (e2:EquitySnapshotNode)
        WHERE NOT e2:KCCNode AND NOT e2:KTMNode AND e2.equity_total IS NOT NULL
        WITH equity_days, e2 ORDER BY e2.snapshot_date DESC LIMIT 1
        MATCH (cfg:TradingConfigNode)
        WHERE NOT cfg:KCCNode AND NOT cfg:KTMNode
        WITH equity_days,
             toFloat(e2.equity_total) AS latest_equity,
             toFloat(cfg.paper_starting_capital) AS capital_base LIMIT 1
        RETURN equity_days, latest_equity, capital_base,
               (latest_equity / capital_base) - 1.0 AS cum_return
    """,

    # ─── KCC STORM ENGINE (peril layers; tenant :StormNode, read-only) ───────
    # Separate tenant on the same Neo4j. Every storm query positively matches
    # :StormNode AND excludes the SignalDelta tenants (KCC/KTM/Trade), so these
    # can never read or cross into trading data. Read-only, whitelisted.
    #
    # Live hail-monitor heartbeat (the MonitorStatus singleton the daemon writes).
    "storm_engine_status": """
        MATCH (n:StormNode:MonitorStatus {status_id: 'MONITOR-hail'})
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
        RETURN n.status_id AS status_id, n.peril AS peril,
               n.last_run_at AS last_run_at, n.last_run_date AS last_run_date,
               n.last_status AS last_status,
               n.last_success_at AS last_success_at, n.last_success_date AS last_success_date,
               n.next_scheduled_at AS next_scheduled_at,
               n.last_alert_at AS last_alert_at, n.last_alert_date AS last_alert_date,
               n.runs_total AS runs_total, n.alerts_total AS alerts_total,
               n.updated_at AS updated_at
    """,

    # TEMPEST forecast-service heartbeat + current active SPC-outlook risk.
    "storm_forecast_status": """
        MATCH (n:StormNode:MonitorStatus {status_id: 'FORECAST'})
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
        RETURN n.status_id AS status_id, n.peril AS peril,
               n.last_run_at AS last_run_at, n.last_status AS last_status,
               n.current_active_risk AS current_active_risk,
               n.next_scheduled_at AS next_scheduled_at,
               n.last_alert_at AS last_alert_at, n.last_alert_date AS last_alert_date,
               n.runs_total AS runs_total, n.alerts_total AS alerts_total,
               n.updated_at AS updated_at
    """,

    # Item 8 STEP 3: heartbeat of the 150mi chase-fill hourly service so the status
    # dashboard can show it green/stale/red alongside monitor + forecast. Read-only,
    # tenant-guarded. (Forecast-shaped: no last_success_at field on this node.)
    "storm_fill_status": """
        MATCH (n:StormNode:MonitorStatus {status_id: 'FILL-150'})
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
        RETURN n.status_id AS status_id, n.peril AS peril,
               n.last_run_at AS last_run_at, n.last_run_date AS last_run_date,
               n.last_status AS last_status,
               n.last_success_at AS last_success_at,
               n.next_scheduled_at AS next_scheduled_at,
               n.last_alert_at AS last_alert_at,
               n.runs_total AS runs_total, n.alerts_total AS alerts_total,
               n.updated_at AS updated_at
    """,

    # Item 9: heartbeat of the daily geofence->250mi chase-DAMAGE alert service, so the
    # status dashboard can show a fourth card green/stale/red. Read-only, tenant-guarded.
    "storm_damage_status": """
        MATCH (n:StormNode:MonitorStatus {status_id: 'DAMAGE-250'})
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
        RETURN n.status_id AS status_id, n.peril AS peril,
               n.last_run_at AS last_run_at, n.last_run_date AS last_run_date,
               n.last_status AS last_status,
               n.last_success_at AS last_success_at,
               n.next_scheduled_at AS next_scheduled_at,
               n.last_alert_at AS last_alert_at,
               n.runs_total AS runs_total, n.alerts_total AS alerts_total,
               n.updated_at AS updated_at
    """,

    # Recent persisted storm runs (one AdCluster per processed date+peril).
    "storm_recent_runs": """
        MATCH (n:StormNode:AdCluster)
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
          AND n.peril = $peril
        RETURN n.storm_date AS storm_date, n.peril AS peril,
               n.circle_count AS circles, n.cover_area_sqmi AS cover_area_sqmi,
               n.gated_swath_area_sqmi AS swath_area_sqmi, n.created_at AS created_at
        ORDER BY n.storm_date DESC LIMIT $limit
    """,

    # One date's results: the AdCluster summary + that date's qualifying ZCTAs.
    "storm_date_results": """
        MATCH (n:StormNode:AdCluster {cluster_id: 'CLUSTER-' + $peril + '-' + $date})
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
        OPTIONAL MATCH (a:StormNode:Area)-[:HAS_LEAD]->(l:StormNode:Lead)
        WHERE NOT a:KCCNode AND NOT a:KTMNode AND NOT a:TradeNode
          AND NOT l:KCCNode AND NOT l:KTMNode AND NOT l:TradeNode
          AND l.lead_id STARTS WITH 'LEAD-' + $peril + '-' + $date + '-'
        RETURN n.storm_date AS storm_date, n.peril AS peril,
               n.circle_count AS circles, n.cover_area_sqmi AS cover_area_sqmi,
               n.gated_swath_area_sqmi AS swath_area_sqmi,
               collect({zip: a.code, zone: a.zone, score: l.score,
                        tier: l.priority_tier})[..250] AS qualifying_zctas
    """,

    # Calendar/date-availability: every processed date + which perils it holds.
    "storm_available_dates": """
        MATCH (n:StormNode:AdCluster)
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
        RETURN n.storm_date AS storm_date, collect(DISTINCT n.peril) AS perils
        ORDER BY storm_date DESC LIMIT 400
    """,

    # Item 10 async PULL poll. READ-ONLY: reads the PullJob lifecycle marker the
    # engine wrote; it can NEVER trigger or re-trigger a compute (only /storm_pull,
    # write-bearer gated, does). The portal polls this on the job_id returned by the
    # fire call until state is terminal (done/empty/error), then refreshes.
    "storm_pull_status": """
        MATCH (n:StormNode:PullJob {job_id: $job_id})
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
        RETURN n.job_id AS job_id, n.state AS state, n.date AS date,
               n.circles AS circles, n.swath_cells AS swath_cells,
               n.cluster_id AS cluster_id, n.error AS error,
               n.started_at AS started_at, n.finished_at AS finished_at,
               n.updated_at AS updated_at
    """,

    # The portal's per-date render payload: one row per peril present, each with
    # the cached circles + swath + evidence blobs (the same data the local viewer
    # computes). Read-only; the portal parses the *_json strings client-side.
    "storm_date_layers": """
        MATCH (n:StormNode:AdCluster {storm_date: $date})
        WHERE NOT n:KCCNode AND NOT n:KTMNode AND NOT n:TradeNode
          AND coalesce(n.superseded, false) = false
        RETURN n.peril AS peril, n.circle_count AS circle_count,
               n.cover_area_sqmi AS cover_area_sqmi,
               n.gated_swath_area_sqmi AS swath_area_sqmi,
               n.zip_union_area_sqmi AS zip_union_area_sqmi,
               n.circles_json AS circles_json, n.swath_json AS swath_json,
               n.evidence_json AS evidence_json,
               n.coverage_zone AS coverage_zone, n.in_geofence AS in_geofence,
               // additive (2026-07-12) — portal chips: tornado gate verdict + miss-recovery review
               // flag + the deferred pricing-incomplete flag. Absent on older nodes -> coalesced.
               coalesce(n.fundable, false) AS fundable,
               n.not_promotable_reason AS not_promotable_reason,
               coalesce(n.miss_recovery, false) AS miss_recovery,
               coalesce(n.passes_incomplete, false) AS passes_incomplete,
               n.exposure_basis AS exposure_basis
        ORDER BY peril, coverage_zone
    """,
}


# Queries into which main.run_query() auto-injects PORTAL_TRADE_CUTOFF_ISO
# as $cutoff. Listed explicitly so the auto-inject is a deliberate opt-in,
# not a side-effect of every Cypher containing the literal string $cutoff.
CUTOFF_QUERIES = frozenset({
    "account_bar",
    "open_positions",
    "trade_list_recent",
    "trade_list_window",
    "trades_closed_day",
    "win_rate",
    "lane2_delta",
    "conviction_tiers",
    "equity_curve_series",
    "equity_curve_stats",
    "returns_matrix_cell",
    "returns_matrix_sigma_row",
    "returns_matrix_sigma_col",
    "returns_matrix_sigma_corner",
    "scanner_scores",
})


# Queries into which main.run_query() auto-injects effective_forensic_ids()
# as $forensic_ids. Every portal query whose Cypher touches TradeNode for
# display/analytics. (sharpe_ratio + equity_curve_* read WeeklyContextNode /
# EquitySnapshotNode aggregates, not TradeNode, so forensic exclusion can't
# apply at the portal boundary — those would need an engine-side recompute
# that excludes the forensic trades from the aggregate node writers.)
FORENSIC_QUERIES = frozenset({
    "account_bar",
    "trade_list_recent",
    "trade_list_window",
    "trades_closed_day",
    "win_rate",
    "lane2_delta",
    "conviction_tiers",
    "returns_matrix_cell",
    "returns_matrix_sigma_row",
    "returns_matrix_sigma_col",
    "returns_matrix_sigma_corner",
})


# Queries into which main.run_query() auto-injects effective_corrupt_exclude_ids()
# as $corrupt_ids — the §6.6 all-time $-panels. Portal never sends the list;
# it is a server-side exclusion policy (the 36 frozen trigger-copy closes).
CORRUPT_EXCLUDE_QUERIES = frozenset({
    "panel_pnl_by_track",
    "panel_returns_by_domain",
    "panel_returns_by_domain_pct",
    "panel_profit_factor",
    "panel_expectancy",
    "panel_sharpe_excl_corrupt",
    "panel_pf_expectancy_series",
})


# Per-query expected parameter keys (for input validation).
# `cutoff` is NOT listed — it's auto-injected by main.py for CUTOFF_QUERIES.
REQUIRED_PARAMS = {
    "account_bar": [],
    "weekly_waterfall": [],
    "open_positions": [],
    "trade_list_recent": [],
    "trade_list_window": ["window_start"],
    "trades_closed_day": ["day_start", "day_end"],
    "recent_events": [],
    "win_rate": [],
    "sharpe_ratio": [],
    "lane2_delta": [],
    "conviction_tiers": [],
    "kernel_nodes": [],
    "kernel_edges": [],
    "equity_curve_series": [],
    "equity_curve_stats": [],
    "returns_matrix_cell": ["asset_class", "track"],
    "returns_matrix_sigma_row": ["track"],
    "returns_matrix_sigma_col": ["asset_class"],
    "returns_matrix_sigma_corner": [],
    "rules_this_week": [],
    "rules_footer": [],
    "closest_cohort": [],
    "monitored_assets": [],
    "trade_overlay_enrichment": ["request_id"],
    "diag_trade_counts": [],
    "diag_realized_pnl": [],
    "diag_equity_snapshots": [],
    "diag_capital_flows": [],
    "diag_equity_nodes": [],
    "engine_heartbeat": [],
    "news_ticker_recent": [],
    "scanner_scores": ["asset_list"],
    "scanner_live_state": [],
    "equity_snapshot_latest": [],
    "account_state": [],
    "account_health_history": [],
    # §6.6 $-panels: $corrupt_ids is auto-injected (CORRUPT_EXCLUDE_QUERIES),
    # not portal-supplied — so no required portal params.
    "panel_pnl_by_track": [],
    "panel_returns_by_domain": [],
    "panel_profit_factor": [],
    "panel_expectancy": [],
    "panel_sharpe_excl_corrupt": [],
    # window ∈ {current_month, ytd, 1y, 5y, all} — portal-supplied selector;
    # $corrupt_ids auto-injected (CORRUPT_EXCLUDE_QUERIES).
    "panel_returns_by_domain_pct": ["window"],
    "panel_annualized_return": [],
    "panel_pf_expectancy_series": [],
    # KCC storm engine
    "storm_engine_status": [],
    "storm_forecast_status": [],
    "storm_fill_status": [],
    "storm_damage_status": [],
    "storm_recent_runs": ["peril", "limit"],
    "storm_date_results": ["peril", "date"],
    "storm_available_dates": [],
    "storm_date_layers": ["date"],
    "storm_pull_status": ["job_id"],
}

assert set(QUERIES.keys()) == set(REQUIRED_PARAMS.keys()), \
    "QUERIES and REQUIRED_PARAMS must have identical keys"
