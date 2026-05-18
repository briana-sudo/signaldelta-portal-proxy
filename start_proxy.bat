@echo off
REM ─────────────────────────────────────────────────────────────
REM SignalDelta portal proxy — Windows launcher.
REM Creates a Python venv if missing, installs dependencies, runs uvicorn.
REM Listens on 127.0.0.1:8000 — TryCloudflare tunnel proxies the public URL.
REM ─────────────────────────────────────────────────────────────

setlocal

cd /d "%~dp0"

if not exist .env (
  echo [proxy] ERROR: .env not found.
  echo [proxy] Copy .env.example to .env and fill in PROXY_API_TOKEN + NEO4J_PASSWORD before starting.
  exit /b 1
)

if not exist .venv (
  echo [proxy] Creating Python venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [proxy] ERROR: failed to create venv. Is Python 3.10+ installed and on PATH?
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

echo [proxy] Installing/updating dependencies ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [proxy] ERROR: pip install failed.
  exit /b 1
)

echo.
echo [proxy] Starting FastAPI on http://127.0.0.1:8000
echo [proxy] In a separate terminal, run start_tunnel.bat to expose this over HTTPS.
echo [proxy] Press Ctrl+C to stop.
echo.

python -m uvicorn main:app --host 127.0.0.1 --port 8000

endlocal
