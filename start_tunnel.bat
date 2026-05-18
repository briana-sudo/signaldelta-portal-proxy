@echo off
REM ─────────────────────────────────────────────────────────────
REM TryCloudflare quick tunnel — exposes local proxy over HTTPS.
REM Requires cloudflared.exe on PATH. Install:
REM   winget install --id Cloudflare.cloudflared
REM or download from https://github.com/cloudflare/cloudflared/releases
REM
REM No authentication needed for quick tunnel mode. The public URL is
REM printed to the console output (look for a line like
REM   https://<random>.trycloudflare.com
REM ) and is regenerated every time you restart the tunnel.
REM Set VITE_PROXY_URL in the portal repo's GitHub Secrets to this URL
REM and re-deploy the portal whenever the tunnel restarts.
REM ─────────────────────────────────────────────────────────────

setlocal

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo [tunnel] ERROR: cloudflared not found on PATH.
  echo [tunnel] Install with:   winget install --id Cloudflare.cloudflared
  echo [tunnel] or from https://github.com/cloudflare/cloudflared/releases
  exit /b 1
)

echo [tunnel] Starting TryCloudflare quick tunnel pointing at http://localhost:8000
echo [tunnel] Watch the output below for a line like:
echo [tunnel]     https://something-something.trycloudflare.com
echo [tunnel] That URL is what you set as VITE_PROXY_URL in the portal's GitHub Secrets.
echo [tunnel] The URL changes every time you restart this command.
echo.

cloudflared tunnel --url http://localhost:8000

endlocal
