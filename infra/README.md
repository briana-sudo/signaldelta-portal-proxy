# Phase 3d-iii-a — prepared infra (operator runs; secrets stay operator-side)

Code prepared these; **the operator supplies the secret VALUES and runs them.** No
secret value is in any file here — only labeled slots. Nothing here has been applied.

## Operator steps (in order)

1. **Provision 7688** — run `provision_neo4j_7688.ps1`. Starts a *second* Neo4j
   **Community** instance on **7688** with its own data dir/config, separate from
   the 7687 trading engine (§6 isolation). Then set `SM_NEO4J_PASSWORD` in the
   proxy `.env` and confirm `/sm/health` reports `provisioned: true, reachable: true`.

2. **Apply Cloudflare Access + tunnel** — paste your Cloudflare API token into the
   `CLOUDFLARE_API_TOKEN` slot in `.env` (or pass `-Token`), then run
   `apply_access_and_tunnel.ps1`. It applies the Access policy in
   `cloudflare_access.json` (auth off the client — gates the whole surface) and the
   ingress edit in `cloudflared_tunnel_config.yml` (existing **named** tunnel — no
   new tunnel, **no DNS change**). Idempotent: re-running is a no-op if already applied.
   **Clear the token from `.env` after applying.**

3. **Data-source keys** are pasted later, at onboarding runtime, into the
   data-needs onboarding field (server-side `SecretsStore`) — not here.

4. **Restart** the `SignalDeltaProxy` NSSM service **after** reviewing the
   feature-branch changes. (Code did not restart it.)

## Guardrails honored by these scripts
- No `git pull` on the proxy clone; feature branch only.
- **No DNS record change** and **no new/deleted tunnel** — `cloudflared_tunnel_config.yml`
  edits ingress on the *existing* named tunnel only.
- No secret value in any file — only labeled slots + a token parameter read at apply time.
- Scripts are idempotent and support `-DryRun` (print the plan, change nothing).
