@echo off
REM Run the automated test suite (offline; no camera, model file or network needed).
REM Extra arguments go to pytest, e.g. scripts\run_tests.bat tests\test_dashboard.py
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Run scripts\setup_windows.bat first
    exit /b 1
)
"%PY%" -m pytest -q %*
endlocal & exit /b %ERRORLEVEL%
