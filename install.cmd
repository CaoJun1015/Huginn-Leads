@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3.11 -m venv .venv
if errorlevel 1 if not exist ".venv\Scripts\python.exe" "D:\Software\nonebota2\.venv\Scripts\python.exe" -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
if errorlevel 1 exit /b 1
pushd frontend
call npm install
if errorlevel 1 exit /b 1
call npm run build
popd
if errorlevel 1 exit /b 1
echo Huginn Leads installed.
