# Setup the SM_ProxyHelper service (run elevated by "Setup Proxy Helper.bat").
# Installs a tiny always-on service whose only job is to restart the proxy on
# request - so the portal "Restart proxy" button works in every case, including the
# first restart after a proxy code change. One-time; after this, no terminal ever.
#
# Native nssm calls print to stderr on benign cases (e.g. stopping a service that
# does not exist yet on a first install), so we DON'T abort on those - the real
# success signal is the helper answering on 127.0.0.1:8199 at the end.
$ErrorActionPreference = "Continue"

try {
    $svc      = "SM_ProxyHelper"
    $dir      = Split-Path -Parent $MyInvocation.MyCommand.Path
    $nssm     = "C:\SignalDelta_Local\tools\nssm.exe"
    $helper   = Join-Path $dir "sm_helper.py"
    $env_file = Join-Path $dir ".env"

    Write-Host "=== SignalDelta - Proxy Helper setup ===" -ForegroundColor Cyan

    # python: prefer the proxy venv, else system python
    $py = Join-Path $dir ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $py)              { throw "No python found ($dir\.venv\Scripts\python.exe or PATH)." }
    if (-not (Test-Path $nssm))   { throw "nssm not found at $nssm." }
    if (-not (Test-Path $helper)) { throw "sm_helper.py not found at $helper." }

    # machine-generated token (never shown/sent) shared between helper and proxy
    $tok = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })

    # idempotent: only touch a prior install if the service actually exists
    if (Get-Service $svc -ErrorAction SilentlyContinue) {
        Write-Host "[helper] removing prior install ..."
        & $nssm stop $svc | Out-Null
        & $nssm remove $svc confirm | Out-Null
        Start-Sleep -Seconds 1
    }

    Write-Host "[helper] installing service $svc ..."
    & $nssm install $svc $py $helper | Out-Null
    & $nssm set $svc AppDirectory $dir | Out-Null
    & $nssm set $svc AppEnvironmentExtra "SM_HELPER_TOKEN=$tok" "SM_PROXY_SERVICE=SignalDeltaProxy" "SM_HELPER_PORT=8199" "SM_HELPER_HOST=127.0.0.1" "SM_NSSM_PATH=$nssm" | Out-Null
    & $nssm set $svc Start SERVICE_AUTO_START | Out-Null
    & $nssm set $svc ObjectName LocalSystem | Out-Null
    & $nssm set $svc AppStdout (Join-Path $dir "sm_helper.log") | Out-Null
    & $nssm set $svc AppStderr (Join-Path $dir "sm_helper.log") | Out-Null

    # share the token with the proxy so it can auth to the helper (idempotent .env edit)
    if (Test-Path $env_file) {
        $lines = @(Get-Content $env_file | Where-Object { $_ -notmatch '^\s*SM_HELPER_(TOKEN|URL)\s*=' })
    } else { $lines = @() }
    $lines += "SM_HELPER_TOKEN=$tok"
    $lines += "SM_HELPER_URL=http://127.0.0.1:8199"
    Set-Content -Path $env_file -Value $lines -Encoding utf8

    Write-Host "[helper] starting service ..."
    & $nssm start $svc | Out-Null
    Start-Sleep -Seconds 2

    # confirm the helper answers (the real success signal)
    $ok = $false
    for ($i = 0; $i -lt 12; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:8199/helper/health"
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }

    Write-Host ""
    if ($ok) {
        Write-Host "HELPER READY" -ForegroundColor Green
        Write-Host "The 'Restart proxy' button now drives every restart - including the first one"
        Write-Host "after a future proxy code change. No terminal needed again."
        Write-Host "(The proxy picks up the shared token on its next restart; the button handles that.)"
    } else {
        Write-Host "Helper installed but did not answer on 127.0.0.1:8199 yet." -ForegroundColor Yellow
        Write-Host "See $dir\sm_helper.log. You can re-run this setup safely."
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
