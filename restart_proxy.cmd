@echo off
REM One-shot proxy restart, run by the SM_ProxyRestart Scheduled Task (SYSTEM),
REM OUTSIDE the proxy's NSSM job so it survives the service stopping. The brief
REM pause lets the /sm/proxy/restart HTTP response flush before the service dies.
REM Controls the SERVICE only — no firewall change, no trade path.
timeout /t 2 /nobreak >nul
set "NSSM=C:\SignalDelta_Local\tools\nssm.exe"
set "SVC=SignalDeltaProxy"
if exist "%NSSM%" (
  "%NSSM%" restart "%SVC%"
) else (
  sc.exe stop "%SVC%"
  timeout /t 4 /nobreak >nul
  sc.exe start "%SVC%"
)
