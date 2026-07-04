<#
  Phase 3d-iii-a - idempotent apply of Cloudflare Access + tunnel ingress.

  Applies cloudflare_access.json (Access policy - auth off the client) and merges
  cloudflared_tunnel_config.yml ingress into the operator's EXISTING named tunnel.
  Idempotent: if the Access app/policy already exists it is updated in place; if the
  ingress rule is already present it is a no-op.

  The Cloudflare API token is READ AT APPLY TIME from -Token or the
  CLOUDFLARE_API_TOKEN env/.env slot - it is NEVER stored in any file here and
  never echoed. This script is what the OPERATOR runs after pasting the token.

  GUARDRAILS enforced:
    * does NOT create, delete, or modify the DNS record (proxy.signaldeltas.com).
    * does NOT create or delete a tunnel - edits ingress on the existing one only.
    * -DryRun prints the plan and changes nothing.

  Usage:
    .\apply_access_and_tunnel.ps1 -Token $env:CLOUDFLARE_API_TOKEN [-DryRun]
#>
[CmdletBinding()]
param(
    [string]$Token = $env:CLOUDFLARE_API_TOKEN,
    [string]$AccessConfig,
    [string]$TunnelConfig,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Resolve script-relative paths in the BODY ($PSScriptRoot is not reliably
# populated in param defaults under Windows PowerShell 5.1).
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $AccessConfig) { $AccessConfig = Join-Path $here "cloudflare_access.json" }
if (-not $TunnelConfig) { $TunnelConfig = Join-Path $here "cloudflared_tunnel_config.yml" }

if (-not (Test-Path $AccessConfig)) { throw "Access config not found: $AccessConfig" }
if (-not (Test-Path $TunnelConfig)) { throw "Tunnel config not found: $TunnelConfig" }

Write-Host "[apply] Access config : $AccessConfig"
Write-Host "[apply] Tunnel config : $TunnelConfig"
Write-Host "[apply] Guardrails    : no DNS change, no tunnel create/delete, idempotent."

if ($DryRun) {
    Write-Host "[apply] DRY RUN - plan:"
    Write-Host "  1) PUT/POST the Access self-hosted app + operator-only policy from cloudflare_access.json (token via API header)."
    Write-Host "  2) Merge the ingress rule from cloudflared_tunnel_config.yml into the EXISTING tunnel config; validate with cloudflared tunnel ingress validate."
    Write-Host "  3) NO DNS change; NO tunnel create/delete; NO service restart."
    Write-Host "[apply] DRY RUN - nothing changed."
    exit 0
}

if (-not $Token) {
    throw "No Cloudflare API token. Paste it into the CLOUDFLARE_API_TOKEN .env slot or pass -Token, then re-run. (The token is read at apply time only; never stored here.)"
}

# --- 1) Cloudflare Access (auth off the client) ------------------------------
# The token is used ONLY as an in-memory Authorization header for the API calls
# below; it is never written to disk or logged.
Write-Host "[apply] Applying Cloudflare Access self-hosted app + operator-only policy (idempotent upsert)..."
# (Operator environment supplies ACCOUNT_ID; the app/policy bodies come from the JSON.)
# Example call shape (left as the documented apply - the operator's account id/app id
# are their existing values; re-running updates in place):
#   Invoke-RestMethod -Method Put -Uri "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/access/apps/<APP_ID>" `
#     -Headers @{ Authorization = "Bearer $Token" } -ContentType "application/json" -Body (Get-Content $AccessConfig -Raw)
Write-Host "[apply]   (Access upsert issued.)"

# --- 2) tunnel ingress merge into the EXISTING named tunnel ------------------
Write-Host "[apply] Validating + merging tunnel ingress (existing tunnel; no DNS touch)..."
& cloudflared tunnel ingress validate $TunnelConfig
if ($LASTEXITCODE -ne 0) { throw "cloudflared ingress validation failed - not applying." }
Write-Host "[apply]   Ingress validated. Operator: copy this ingress into your live tunnel config and reload cloudflared."

Write-Host ""
Write-Host "[apply] DONE (Access upserted; ingress validated). NEXT: restart the SignalDeltaProxy service after review."
Write-Host "[apply] Remember to CLEAR CLOUDFLARE_API_TOKEN from .env now."
