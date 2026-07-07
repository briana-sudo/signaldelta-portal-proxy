# Setup the SM_ProxyFront service (run elevated by "Setup Proxy Front.bat").
#
# WHAT IT DOES (the graceful-restart rollout):
#   1. Repoints the FastAPI app (SignalDeltaProxy) from :8000 -> :8001.
#   2. Installs an always-up front reverse proxy (SM_ProxyFront) on :8000 that
#      upstreams to the app on :8001 and HOLDS requests across an app restart
#      instead of surfacing a 502.
#   cloudflared is UNCHANGED — it still points at :8000, which is now the front.
#
# After this, an "Update & restart" bounces only the app on :8001; the front on
# :8000 stays up, so the browser never sees a 502 during a deploy. One-time; safe
# to re-run (idempotent).
#
# nssm prints to stderr on benign cases, so we DON'T abort on those — the real
# success signal is the front answering on :8000 and proxying /health at the end.
$ErrorActionPreference = "Continue"

try {
    $front  = "SM_ProxyFront"
    $proxy  = "SignalDeltaProxy"
    $dir    = Split-Path -Parent $MyInvocation.MyCommand.Path
    $nssm   = "C:\SignalDelta_Local\tools\nssm.exe"
    $script = Join-Path $dir "sm_proxy_front.py"

    Write-Host "=== SignalDelta - Proxy Front (graceful restart) setup ===" -ForegroundColor Cyan

    $py = Join-Path $dir ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $py)                { throw "No python found ($dir\.venv\Scripts\python.exe or PATH)." }
    if (-not (Test-Path $nssm))  { throw "nssm not found at $nssm." }
    if (-not (Test-Path $script)){ throw "sm_proxy_front.py not found at $script." }
    if (-not (Get-Service $proxy -ErrorAction SilentlyContinue)) { throw "$proxy is not installed — run the proxy setup first." }

    # 1) Repoint the app to :8001 and restart it there (frees :8000 for the front).
    Write-Host "[front] repointing $proxy to 127.0.0.1:8001 ..."
    & $nssm set $proxy AppParameters "-m uvicorn main:app --host 127.0.0.1 --port 8001" | Out-Null
    & $nssm restart $proxy | Out-Null
    Start-Sleep -Seconds 3

    # 2) Install the front on :8000 -> app :8001.
    if (Get-Service $front -ErrorAction SilentlyContinue) {
        Write-Host "[front] removing prior install ..."
        & $nssm stop $front | Out-Null
        & $nssm remove $front confirm | Out-Null
        Start-Sleep -Seconds 1
    }
    Write-Host "[front] installing service $front ..."
    & $nssm install $front $py $script | Out-Null
    & $nssm set $front AppDirectory $dir | Out-Null
    & $nssm set $front AppEnvironmentExtra "SM_FRONT_HOST=127.0.0.1" "SM_FRONT_PORT=8000" "SM_APP_HOST=127.0.0.1" "SM_APP_PORT=8001" | Out-Null
    & $nssm set $front Start SERVICE_AUTO_START | Out-Null
    & $nssm set $front ObjectName LocalSystem | Out-Null
    & $nssm set $front AppStdout (Join-Path $dir "sm_front.log") | Out-Null
    & $nssm set $front AppStderr (Join-Path $dir "sm_front.log") | Out-Null

    Write-Host "[front] starting service ..."
    & $nssm start $front | Out-Null
    Start-Sleep -Seconds 2

    # 3) Verify: the front answers on :8000, AND it proxies the app's /health through.
    $frontOk = $false; $throughOk = $false
    for ($i = 0; $i -lt 12; $i++) {
        try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:8000/_front/health").StatusCode -eq 200) { $frontOk = $true; break } }
        catch { Start-Sleep -Seconds 1 }
    }
    for ($i = 0; $i -lt 12; $i++) {
        try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:8000/health").StatusCode -eq 200) { $throughOk = $true; break } }
        catch { Start-Sleep -Seconds 1 }
    }

    Write-Host ""
    if ($frontOk -and $throughOk) {
        Write-Host "FRONT READY — graceful restart active." -ForegroundColor Green
        Write-Host "cloudflared still points at :8000 (now the front); the app runs on :8001."
        Write-Host "An Update & restart now bounces only :8001 — the browser no longer 502s during a deploy."
    } else {
        Write-Host ("Front install incomplete (front={0}, through={1})." -f $frontOk, $throughOk) -ForegroundColor Yellow
        Write-Host "See $dir\sm_front.log and the proxy log. Re-run this setup safely, or roll back with:"
        Write-Host "  nssm stop $front; nssm remove $front confirm"
        Write-Host "  nssm set $proxy AppParameters `"-m uvicorn main:app --host 127.0.0.1 --port 8000`"; nssm restart $proxy"
    }
}
catch {
    Write-Host ""
    Write-Host ("SETUP FAILED: " + $_.Exception.Message) -ForegroundColor Red
}
finally {
    Write-Host ""
    Read-Host "Press Enter to close"
}
