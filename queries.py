"""
Cypher whitelist for the SignalDelta portal proxy.

Portal callers POST {"name": "<query_name>", "params": {...}} to /query. The
proxy looks up the Cypher string here by name. Portal can never inject
arbitrary Cypher — only the 20 pre-authored named queries below are callable.

Sources:
  - Original reconciliation §Panel-by-Panel + §Consolidated 60-Second Poll
  - Reconciliation v1.1+ Section D (D1 Equity Curve, D2 Returns Matrix, D3 Rules)
  - Section H punchlist fields (TradeNode.asset_class, TradeNode.phase,
    EquitySnapshotNode TWR/PEAK/DD) written against documented field names;
    queries return empty until §14 / §17 amendments land. Portal's bootstrap-
    state pattern covers the gap automatically on the next poll.

Phase filter (Section E mode toggle): not applied here in Phase 1.1 since
TradeNode.phase isn't yet a §14 field. When it lands, append the WHERE clause
inside execute_query() in main.py based on a `mode` param from the request.
"""

QUERIES = {
    # ── Panel-by-panel set (10) ───────────────────────────────────────────
    "account_bar": """
        MATCH (c:TradingConfigNode)
        WITH c.paper_starting_capital AS capital_base, c.current_phase AS current_phase
        OPTIONAL MATCH (e:EquitySnapshotNode)
        WITH capital_base, current_phase, e ORDER BY e.snapshot_date DESC LIMIT 1
        OPTIONAL MATCH (t_all:TradeNode)
        WITH capital_base, current_phase, e, count(t_all) AS trade_count
        OPTIONAL MATCH (t_open:TradeNode {status: 'OPEN'})
        RETURN capital_base,
               current_phase,
               e.equity_total AS current_value,
               e.dollar_pnl_today AS today_pnl,
               e.percent_pnl_today AS today_pnl_pct,
               e.sync_timestamp AS last_sync,
               trade_count,
               count(t_open) AS open_count
    """,

    "weekly_waterfall": """
        MATCH (w:WeeklyContextNode)
        RETURN w.week_start_date AS week_start,
               w.system_weekly_pnl_pct AS pnl_pct
        ORDER BY w.week_start_date DESC
        LIMIT 6
    """,

    "open_positions": """
        MATCH (t:TradeNode {status: 'OPEN'})
        RETURN t.request_id AS request_id,
               t.asset AS asset,
               t.track AS track,
               t.conviction_tier AS conviction,
               t.entry_price AS entry_price,
               t.stop_loss_price AS stop_price,
               t.target_price AS target_price,
               t.direction AS direction,
               t.entry_timestamp AS entry_timestamp
        ORDER BY t.entry_timestamp DESC
        LIMIT 3
    """,

    "recent_events": """
        MATCH (e:SystemEventNode)
        WHERE e.timestamp >= datetime() - duration('PT30M')
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

    "win_rate": """
        MATCH (t:TradeNode {status: 'CLOSED'})
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

    "lane2_delta": """
        MATCH (t:TradeNode {status: 'CLOSED'})
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

    # Conviction tier distribution.
    #
    # Neo4j 5.x rejects the inline expression `(count(t) * 1.0 / total) * 100`
    # in RETURN: it mixes the aggregation count(t) with the non-aggregated
    # scalar `total` from a preceding WITH inside a single expression
    # ("Aggregation column contains implicit grouping expressions", gql 42001).
    # Fix: extract count(t) into a preceding WITH so RETURN does pure scalar
    # arithmetic on already-resolved variables.
    "conviction_tiers": """
        MATCH (t:TradeNode)
        WITH count(t) AS total
        MATCH (t:TradeNode)
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

    # ── Section D1: Equity Curve ──────────────────────────────────────────
    "equity_curve_series": """
        MATCH (e:EquitySnapshotNode)
        WHERE e.snapshot_date >= date() - duration({days: 60})
        RETURN e.snapshot_date AS snapshot_date,
               e.equity_total AS equity
        ORDER BY e.snapshot_date ASC
    """,

    "equity_curve_stats": """
        MATCH (e:EquitySnapshotNode)
        RETURN e.peak_equity_to_date AS peak,
               e.max_drawdown_to_date_percent AS drawdown_pct,
               e.twr_to_date_percent AS twr_pct,
               e.snapshot_date AS as_of
        ORDER BY e.snapshot_date DESC
        LIMIT 1
    """,

    # ── Section D2: Returns by Domain matrix ──────────────────────────────
    # Cell — full filter (asset_class × track)
    "returns_matrix_cell": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE t.asset_class = $asset_class
          AND t.track = $track
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    # Σ row — per-track aggregation across all asset classes (drop asset_class filter)
    "returns_matrix_sigma_row": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE t.track = $track
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    # Σ column — per-asset-class aggregation across all tracks (drop track filter)
    "returns_matrix_sigma_col": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE t.asset_class = $asset_class
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    # Σ corner — system-wide TOTAL (drop both filters)
    "returns_matrix_sigma_corner": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    # ── Section D3: Rules Added This Week ─────────────────────────────────
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

    # ── Mount-time + per-event ────────────────────────────────────────────
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

    # ── Diagnostics — equity drift anomaly verification (2026-05-26) ──
    # Read-only Cypher matching the operator's Q1-Q5 verbatim. KCCNode
    # exclusion is preserved to filter out the other-namespace nodes that
    # share label vocabulary with the engine. These remain in the whitelist
    # past the immediate verification — they're useful general health checks
    # for the engine's TradeNode / EquitySnapshotNode / CapitalFlowNode state.

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
}

# Per-query expected parameter keys (for input validation).
# Empty list = no params required.
REQUIRED_PARAMS = {
    "account_bar": [],
    "weekly_waterfall": [],
    "open_positions": [],
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
    "monitored_assets": [],
    "trade_overlay_enrichment": ["request_id"],
    "diag_trade_counts": [],
    "diag_realized_pnl": [],
    "diag_equity_snapshots": [],
    "diag_capital_flows": [],
    "diag_equity_nodes": [],
}

# Sanity check at import time.
assert set(QUERIES.keys()) == set(REQUIRED_PARAMS.keys()), \
    "QUERIES and REQUIRED_PARAMS must have identical keys"
