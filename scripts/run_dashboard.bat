@echo off
REM Launch the Streamlit conductor dashboard (reads data\passengers.db).
REM Run this in a second terminal while scripts\run_detection.bat is running.
REM Extra arguments go to the dashboard, e.g.: scripts\run_dashboard.bat --db data\passengers.db
setlocal
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM Bound to localhost only: the dashboard has no login.
"%PY%" -m streamlit run app\dashboard\dashboard.py --server.address 127.0.0.1 --server.port 8501 -- %*
endlocal
