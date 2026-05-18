# SignalDelta Portal Proxy

Local-Neo4j → HTTPS bridge for the SignalDelta portal. The engine writes to local Neo4j on `bolt://localhost:7687` per v3 §15; the portal lives on GitHub Pages and cannot reach `localhost`. This proxy runs on the XPS 15 alongside the engine, accepts whitelisted queries from the portal over HTTPS via a TryCloudflare quick tunnel, executes them against local Neo4j, and returns JSON.

## Architecture

```
┌─────────────────────────┐
│ Portal (GitHub Pages)   │  https://briana-sudo.github.io/signaldelta-portal/
└───────────┬─────────────┘
            │ POST /query  { name, params }  (Authorization: Bearer <token>)
            ▼
┌─────────────────────────┐
│ TryCloudflare tunnel    │  https://<rotating>.trycloudflare.com
└───────────┬─────────────┘
            │ http://127.0.0.1:8000
            ▼
┌─────────────────────────┐
│ FastAPI proxy (this)    │  Whitelist: 20 named Cypher queries
└───────────┬─────────────┘
            │ bolt://localhost:7687
            ▼
┌─────────────────────────┐
│ Local Neo4j (engine DB) │  v3 §15 system of record
└─────────────────────────┘
```

## Security model

- **Bearer auth.** Every request must include `Authorization: Bearer <PROXY_API_TOKEN>`. Token lives in two places only: the operator's local `.env` and the portal's GitHub Secret `VITE_PROXY_API_TOKEN`.
- **Cypher whitelist.** The portal posts a `name` and `params`, not a Cypher string. Twenty pre-authored queries in `queries.py` are the only callable surface. SQL/Cypher injection is impossible by construction.
- **CORS.** Only `https://briana-sudo.github.io` (plus localhost dev) is allowed by browsers via the CORSMiddleware. Bearer auth is the actual security boundary; CORS just keeps browsers from blocking the preflight.
- **Read-only.** Queries use `default_access_mode="READ"` against Neo4j. The proxy cannot write.
- **Rotating tunnel URL.** TryCloudflare quick tunnels generate a fresh `<random>.trycloudflare.com` URL on every start. The operator updates `VITE_PROXY_URL` in the portal's GitHub Secrets and re-deploys the portal whenever the tunnel restarts. Long-running deploys use a named tunnel (out of scope here).

## Prerequisites

- Python 3.10 or newer on PATH (`python --version`)
- Local Neo4j running on `bolt://localhost:7687`
- `cloudflared` on PATH for the tunnel script (`winget install --id Cloudflare.cloudflared` or [GitHub Releases](https://github.com/cloudflare/cloudflared/releases))

## Setup

1. Clone or download this repo to the XPS 15.
2. Copy `.env.example` → `.env` and fill in real values:
   - `PROXY_API_TOKEN` — paste the 32-character token generated for your install. The same token must be set as `VITE_PROXY_API_TOKEN` on the portal repo's GitHub Secrets.
   - `NEO4J_PASSWORD` — your local Neo4j password.
   - Leave `NEO4J_URI` and `NEO4J_USER` at defaults unless you customized the local install.
3. Run `start_proxy.bat`. First run creates a `.venv`, installs `fastapi`/`uvicorn`/`neo4j`/`pydantic`/`python-dotenv`, then launches uvicorn on `127.0.0.1:8000`. Subsequent runs reuse the venv and skip pip if already up to date.
4. In a second terminal, run `start_tunnel.bat`. cloudflared prints a public URL like `https://something-something.trycloudflare.com`. Copy it.
5. Open the portal repo (`signaldelta-portal`) on GitHub → Settings → Secrets and variables → Actions, and set:
   - `VITE_PROXY_URL` = the trycloudflare URL from step 4 (no trailing slash)
   - `VITE_PROXY_API_TOKEN` = the same 32-character token from step 2
6. Trigger a portal redeploy (Actions tab → Deploy to GitHub Pages → Run workflow → main).
7. Verify by opening the portal and watching the SYNC poll indicator complete a cycle. The status should turn green.

## Operational flow

**Every time you start a session at the XPS 15:**

1. Make sure Neo4j is running (Neo4j Desktop or systemctl, however you installed it).
2. Double-click `start_proxy.bat`. Leave the terminal open.
3. Double-click `start_tunnel.bat`. Watch for the `https://….trycloudflare.com` line.
4. If the URL is the same as last session: nothing to do, portal still works.
5. If the URL changed (almost always — TryCloudflare regenerates on every start): update `VITE_PROXY_URL` in the portal's GitHub Secrets and trigger a redeploy. Portal goes live again in ~30 seconds.

**Every time you shut down:**

- Ctrl+C in both terminals. Tunnel goes down. Portal returns to bootstrap-state empty rendering until the next session.

## Rotating URL caveat

The biggest operational drawback of TryCloudflare quick tunnels is that the public URL is regenerated on every start. The portal won't reach the new URL until `VITE_PROXY_URL` is updated and the portal is redeployed. This is acceptable for Phase 1.1 because:

- Engine sessions are bounded (operator at desk, not 24/7 yet)
- Phase 4 will replace this with a named tunnel or a hosted backend
- The portal's bootstrap-state pattern means a stale URL just shows "AWAITING LIVE TRADES" placeholders — no crash, no broken state

If you want a stable URL during a long session, leave both terminals running and the portal stays live.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | none | Service identity + whitelist names |
| GET | `/health` | none | Verifies Neo4j connectivity each call |
| POST | `/query` | Bearer | Execute a whitelisted query by name |

### POST /query

Request:

```json
{
  "name": "account_bar",
  "params": {}
}
```

Headers: `Authorization: Bearer <PROXY_API_TOKEN>` · `Content-Type: application/json`

Response:

```json
{
  "name": "account_bar",
  "row_count": 1,
  "rows": [
    {
      "capital_base": 10000.0,
      "current_phase": "Paper",
      "current_value": 11847.32,
      "today_pnl": 142.18,
      "today_pnl_pct": 1.21,
      "last_sync": "2026-05-18T23:55:01",
      "trade_count": 247,
      "open_count": 3
    }
  ]
}
```

Errors: `400` unknown query name or missing required params · `401`/`403` bad bearer · `500` Neo4j query failed · `503` Neo4j unreachable.

### Whitelisted queries

20 names, sourced from the [Portal Spec Reconciliation v1.2](https://www.notion.so/364ca70abea681a09305e1dda20461e1):

```
account_bar              equity_curve_series         rules_this_week
weekly_waterfall         equity_curve_stats          rules_footer
open_positions           returns_matrix_cell         monitored_assets
recent_events            returns_matrix_sigma_row    trade_overlay_enrichment
win_rate                 returns_matrix_sigma_col
sharpe_ratio             returns_matrix_sigma_corner
lane2_delta              kernel_nodes
conviction_tiers         kernel_edges
```

See `queries.py` for the Cypher and required params per query.

## Phase 4 evolution

When the engine moves to live trading and a named/permanent tunnel or a hosted backend replaces this proxy, the portal's `useNeo4jPoll` hook flips `VITE_PROXY_URL` to the new endpoint. The wire protocol (`POST /query { name, params }` with bearer auth) is stable; only the URL changes. The query whitelist stays the same. The §14 amendments for `TradeNode.phase` and `TradeNode.asset_class` land independently — queries return empty until they do, and the portal renders bootstrap states gracefully in the interim.

## Status

Phase 1.1 Step D wiring. Authored in tandem with the portal repo's `useNeo4jPoll` hook flip from direct neo4j-driver to fetch-over-proxy. Engine remains untouched.
