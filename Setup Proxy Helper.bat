@echo off
REM ============================================================
REM  SignalDelta - SET UP THE PROXY HELPER (double-click this).
REM
REM  Installs a tiny always-on helper service whose only job is to restart the
REM  proxy when you click "Restart proxy" on the Discovery console. After this
REM  one-time setup, the button drives EVERY restart - including the first one
REM  after a proxy code change - so you never need a terminal for it again.
REM
REM  Double-click this file and approve the UAC prompt. When it says
REM  "HELPER READY", you're done.
REM ============================================================

echo Launching elevated proxy-helper setup (approve the UAC prompt)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0setup_proxy_helper.ps1'"

echo.
echo If a UAC prompt appeared and you approved it, setup is running in a new window.
echo Wait for 'HELPER READY', then close that window.
