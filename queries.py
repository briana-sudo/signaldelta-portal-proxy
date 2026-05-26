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

# Pre-market trade cutoff per Portal v1.1 Change 1. Engine launched at this
# UTC instant on 2026-05-26 (US market open 13:30 UTC); trades fired before
# this point are pre-launch test fires and skew portal metrics. Graph itself
# is unchanged — filter is read-only at the proxy boundary.
PORTAL_TRADE_CUTOFF_ISO = "2026-05-26T13:30:00Z"


QUERIES = {
    # ── Account bar (Change 1: t_all + t_open both cutoff-filtered) ───────
    "account_bar": """
        MATCH (c:TradingConfigNode)
        WITH c.paper_starting_capital AS capital_base, c.current_phase AS current_phase
        OPTIONAL MATCH (e:EquitySnapshotNode)
        WHERE date(e.snapshot_date) >= date(substring($cutoff, 0, 10))
        WITH capital_base, current_phase, e ORDER BY e.snapshot_date DESC LIMIT 1
        OPTIONAL MATCH (t_all:TradeNode)
        WHERE datetime(t_all.entry_timestamp) >= datetime($cutoff)
        WITH capital_base, current_phase, e, count(t_all) AS trade_count
        OPTIONAL MATCH (t_open:TradeNode {status: 'OPEN'})
        WHERE datetime(t_open.entry_timestamp) >= datetime($cutoff)
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

    # Open-positions query retained for any caller still referencing it.
    # Portal v1.1 Change 2 switches the panel to `trade_list_recent` (below).
    "open_positions": """
        MATCH (t:TradeNode {status: 'OPEN'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
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

    # ── Portal v1.1 Change 2: trade list (both OPEN and CLOSED) ───────────
    "trade_list_recent": """
        MATCH (t:TradeNode)
        WHERE NOT t:KCCNode AND NOT t:KTMNode
          AND datetime(t.entry_timestamp) >= datetime($cutoff)
        RETURN t.request_id AS request_id,
               t.asset AS asset,
               t.track AS track,
               t.conviction_tier AS conviction,
               t.entry_price AS entry_price,
               t.exit_price AS exit_price,
               t.stop_loss_price AS stop_price,
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
               t.exit_reason AS exit_reason
        ORDER BY t.entry_timestamp DESC
        LIMIT 12
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
        WITH count(t) AS total
        MATCH (t:TradeNode)
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
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
          AND t.asset_class = $asset_class
          AND t.track = $track
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    "returns_matrix_sigma_row": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND t.track = $track
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    "returns_matrix_sigma_col": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
          AND t.asset_class = $asset_class
        WITH count(t) AS total,
             sum(CASE WHEN t.win_loss = 'Win' THEN 1 ELSE 0 END) AS wins,
             collect(t.pnl_percent) AS returns
        RETURN total, wins, returns
    """,

    "returns_matrix_sigma_corner": """
        MATCH (t:TradeNode {status: 'CLOSED'})
        WHERE datetime(t.entry_timestamp) >= datetime($cutoff)
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
          RETURN max(coalesce(t.exit_timestamp, t.entry_timestamp)) AS ts
          UNION ALL
          MATCH (e:SystemEventNode)
          RETURN max(e.timestamp) AS ts
          UNION ALL
          MATCH (es:EquitySnapshotNode)
          RETURN max(coalesce(es.sync_timestamp, es.created_timestamp)) AS ts
          UNION ALL
          MATCH (a:Layer1AnomalyNode)
          RETURN max(coalesce(a.created_timestamp, a.timestamp)) AS ts
          UNION ALL
          MATCH (r:TradingRuleNode)
          RETURN max(r.created_timestamp) AS ts
          UNION ALL
          MATCH (p:PredictionNode)
          RETURN max(coalesce(p.created_timestamp, p.prediction_timestamp)) AS ts
        }
        RETURN max(ts) AS last_engine_write
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
}


# Queries into which main.run_query() auto-injects PORTAL_TRADE_CUTOFF_ISO
# as $cutoff. Listed explicitly so the auto-inject is a deliberate opt-in,
# not a side-effect of every Cypher containing the literal string $cutoff.
CUTOFF_QUERIES = frozenset({
    "account_bar",
    "open_positions",
    "trade_list_recent",
    "win_rate",
    "lane2_delta",
    "conviction_tiers",
    "equity_curve_series",
    "equity_curve_stats",
    "returns_matrix_cell",
    "returns_matrix_sigma_row",
    "returns_matrix_sigma_col",
    "returns_matrix_sigma_corner",
})


# Per-query expected parameter keys (for input validation).
# `cutoff` is NOT listed — it's auto-injected by main.py for CUTOFF_QUERIES.
REQUIRED_PARAMS = {
    "account_bar": [],
    "weekly_waterfall": [],
    "open_positions": [],
    "trade_list_recent": [],
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
    "engine_heartbeat": [],
    "news_ticker_recent": [],
}

assert set(QUERIES.keys()) == set(REQUIRED_PARAMS.keys()), \
    "QUERIES and REQUIRED_PARAMS must have identical keys"
