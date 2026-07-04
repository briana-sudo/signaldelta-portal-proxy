# Inject ANTHROPIC_API_KEY into the SignalDeltaProxy service env (run elevated by
# "Setup Proxy Key.bat"). The Discovery analyst reads the key from the proxy SERVICE
# ENV only; this is how the operator puts it there.
#
# The key is read MACHINE-SIDE (your ANTHROPIC_API_KEY env var, or an interactive
# paste in this elevated window) and written straight into the service env. It is
# NEVER shown, logged, or read from the trading engine's .env. One-time.
$ErrorActionPreference = "Continue"

try {
    $SERVICE = "SignalDeltaProxy"
    $nssm = "C:\SignalDelta_Local\tools\nssm.exe"
    Write-Host "=== SignalDelta - Proxy analyst key setup ===" -ForegroundColor Cyan

    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
    if (-not $isAdmin) { throw "Not elevated. Double-click 'Setup Proxy Key.bat' and approve UAC." }
    if (-not (Test-Path $nssm)) { throw "nssm not found at $nssm." }
    if (-not (Get-Service $SERVICE -ErrorAction SilentlyContinue)) { throw "$SERVICE service not found." }

    # key: machine env var first; else prompt (stays on this machine, never displayed)
    $key = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
    if (-not $key) { $key = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "Machine") }
    if (-not $key) {
        Write-Host "ANTHROPIC_API_KEY not found in your machine env."
        $sec = Read-Host "Paste your Anthropic API key (stays on this machine; never shown)" -AsSecureString
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
    if (-not $key) { throw "No key provided." }

    # merge into the existing service env (preserve other entries)
    $existing = & $nssm get $SERVICE AppEnvironmentExtra 2>$null
    $map = [ordered]@{}
    foreach ($line in ($existing -split "`r?`n")) {
        $t = ($line -replace "`0", "").Trim()
        if ($t -match '^([A-Za-z0-9_]+)=(.*)$') { $map[$Matches[1]] = $Matches[2] }
    }
    $map["ANTHROPIC_API_KEY"] = $key
    $nargs = @($SERVICE, "AppEnvironmentExtra")
    foreach ($k in $map.Keys) { $nargs += ("{0}={1}" -f $k, $map[$k]) }
    & $nssm @nargs | Out-Null
    Write-Host "  ANTHROPIC_API_KEY injected into $SERVICE service env (value hidden)." -ForegroundColor Green

    Write-Host "[restart] cycling $SERVICE so the analyst picks up the key ..."
    & $nssm restart $SERVICE | Out-Null
    Start-Sleep -Seconds 3
    Write-Host ""
    Write-Host "ANALYST KEY READY. The Discovery analyst now answers with the LLM." -ForegroundColor Green
    Write-Host "(If the key was wrong, re-run this and paste the correct one.)"
}
catch {
    Write-Host ""
    Write-Host ("SETUP FAILED: " + $_.Exception.Message) -ForegroundColor Red
}
finally {
    Write-Host ""
    Read-Host "Press Enter to close" | Out-Null
}
