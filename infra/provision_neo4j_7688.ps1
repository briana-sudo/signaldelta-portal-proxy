<#
  Phase 3d-iii-a - provision the SEARCH-MASTER Neo4j (Community) on 7688.

  Starts a SECOND Neo4j Community instance on bolt 7688 / http 7475, with its own
  data dir + config, entirely separate from the 7687 trading engine (section 6
  instance isolation). Community edition (no RBAC) - the read-only boundary is
  carried by the proxy's three application layers (read-mode sessions + whitelist
  allowlist + ReadOnlyViolation), not a DB role.

  NO secret in this script. The operator sets the initial password interactively
  on first run (neo4j-admin), then puts it in the proxy .env as SM_NEO4J_PASSWORD.

  Usage:
    .\provision_neo4j_7688.ps1 -Neo4jHome "C:\neo4j-sm" [-DryRun]

  This does NOT touch the 7687 instance, its data, or its service.
#>
[CmdletBinding()]
param(
    [string]$Neo4jHome = "C:\neo4j-sm",
    [int]$BoltPort = 7688,
    [int]$HttpPort = 7475,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

Write-Host "[7688] Search-master Neo4j Community provisioning plan:"
Write-Host "  Neo4jHome : $Neo4jHome"
Write-Host "  bolt      : $BoltPort   (7687 trading engine is untouched)"
Write-Host "  http      : $HttpPort"

$confPath = Join-Path $Neo4jHome "conf\neo4j.conf"
$confBody = @"
# Search-master (7688) - Community, isolated from the 7687 trading engine.
server.bolt.listen_address=:$BoltPort
server.http.listen_address=:$HttpPort
server.https.enabled=false
server.directories.data=data
server.directories.logs=logs
# Community has no RBAC; the read-only boundary is the proxy's 3 layers (1.1 Rev 3.2).
dbms.security.auth_enabled=true
"@

if ($DryRun) {
    Write-Host "[7688] DRY RUN - would write conf to $confPath :"
    Write-Host $confBody
    Write-Host "[7688] DRY RUN - would run: neo4j-admin dbms set-initial-password (interactive; no secret in this script)"
    Write-Host "[7688] DRY RUN - would install + start a distinct Windows service 'Neo4j-SM-7688'."
    exit 0
}

if (-not (Test-Path $Neo4jHome)) {
    New-Item -ItemType Directory -Force -Path $Neo4jHome | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Neo4jHome "conf") | Out-Null
}
Set-Content -Path $confPath -Value $confBody -Encoding utf8
Write-Host "[7688] Wrote $confPath"

Write-Host ""
Write-Host "[7688] OPERATOR STEPS (secret stays with you - not in this script):"
Write-Host "  1) Download Neo4j Community (same major as your 7687) into $Neo4jHome if not present."
Write-Host "  2) Set the initial password (interactive):"
Write-Host "       & '$Neo4jHome\bin\neo4j-admin' dbms set-initial-password"
Write-Host "  3) Install + start the isolated service (distinct name from the 7687 service):"
Write-Host "       & '$Neo4jHome\bin\neo4j' windows-service install --service-name Neo4j-SM-7688"
Write-Host "       Start-Service Neo4j-SM-7688"
Write-Host "  4) Put that password in the proxy .env as SM_NEO4J_PASSWORD, then check GET /sm/health."
