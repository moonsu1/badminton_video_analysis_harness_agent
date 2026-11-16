@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" -m badminton.operations stop
"%~dp0.venv\Scripts\python.exe" -m badminton.operations setup
if errorlevel 1 goto end
"%~dp0.venv\Scripts\python.exe" -m badminton.operations start
:end
pause
