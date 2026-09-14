@echo off
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: restore.cmd backups\huginn-leads-YYYYMMDD-HHMMSS.db
  exit /b 2
)
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\restore.py" "%~1"
