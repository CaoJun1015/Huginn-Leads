@echo off
cd /d "%~dp0"
if exist "%~dp0HuginnLeads.exe" (
  "%~dp0HuginnLeads.exe" restore %*
) else (
  "%~dp0.venv\Scripts\python.exe" "%~dp0scripts\restore.py" %*
)
exit /b %errorlevel%
