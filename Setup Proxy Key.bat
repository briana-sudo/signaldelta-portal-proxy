@echo off
REM ============================================================
REM  SignalDelta - SET THE ANALYST KEY (double-click this).
REM
REM  Puts your Anthropic API key into the proxy service so the Discovery analyst
REM  can answer with the LLM. The key is read from your machine env (or you paste
REM  it once, in the elevated window) - it is never shown and never read from the
REM  trading engine's config.
REM
REM  Double-click, approve UAC, (paste the key if asked). When it says
REM  "ANALYST KEY READY", you're done.
REM ============================================================

echo Launching elevated analyst-key setup (approve the UAC prompt)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0setup_proxy_key.ps1'"

echo.
echo If a UAC prompt appeared and you approved it, setup is running in a new window.
echo Paste the key if asked, then wait for 'ANALYST KEY READY'.
