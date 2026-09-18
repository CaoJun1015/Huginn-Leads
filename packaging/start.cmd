@echo off
cd /d "%~dp0"
set "HUGINN_URL=http://127.0.0.1:18080"
if not exist "HuginnLeads.exe" (
  echo Missing HuginnLeads.exe. Please extract the complete ZIP first.
  pause
  exit /b 1
)
echo Huginn Leads: %HUGINN_URL%
echo The browser will open automatically after the service is ready.
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "$u='%HUGINN_URL%'; for($i=0;$i -lt 120;$i++){try{$null=Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 1; Start-Process $u; exit}catch{Start-Sleep -Milliseconds 500}}"
"%~dp0HuginnLeads.exe"
pause
