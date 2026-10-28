@echo off
REM Start the Streamlit conductor dashboard at http://127.0.0.1:8501 (localhost only).
REM It reads the same SQLite database as scripts\run_detection.bat (TPD_DB_PATH or database.path in
REM config\config.yaml). Run it in a second terminal while detection is running.
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Run scripts\setup_windows.bat first
    exit /b 1
)
"%PY%" -m streamlit run dashboard\app.py --server.address 127.0.0.1 --server.port 8501 %*
endlocal & exit /b %ERRORLEVEL%
