@echo off
REM One-time setup on Windows: Python 3.11 virtual environment (.venv), pinned dependencies,
REM the SQLite database (tables only) and a check for the YOLO model file.
REM CPU-only machines are supported; no GPU is required.
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"

if exist "%PY%" goto check_venv
set "BASE="
py -3.11 -c "import sys" >nul 2>&1 && set "BASE=py -3.11"
if defined BASE goto create_venv
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1 && set "BASE=python"
if defined BASE goto create_venv
echo Python 3.11 is required. Install it from https://www.python.org/downloads/ and run this script again.
exit /b 1

:create_venv
echo Creating virtual environment .venv with %BASE% ...
%BASE% -m venv .venv
if errorlevel 1 goto venv_failed
goto install

:check_venv
"%PY%" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)"
if errorlevel 1 goto wrong_venv

:install
if not exist "requirements.txt" goto no_requirements
echo Installing pinned dependencies from requirements.txt ...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto install_failed
"%PY%" -m pip check
if errorlevel 1 goto install_failed

echo Initializing the database ...
"%PY%" scripts\init_db.py
if errorlevel 1 goto db_failed

if exist "models\yolov8n.pt" goto done
echo.
echo [WARN] YOLO model not found: models\yolov8n.pt  (see models\README.md)
echo Download it once with:
echo   cd models
echo   ..\.venv\Scripts\python.exe -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

:done
echo.
echo Setup complete.
echo   1. Start the IP Webcam app on the Android phone (same Wi-Fi network / hotspot as this computer).
echo   2. scripts\run_detection.bat            (default source: phone camera http://192.0.0.4:8080/video)
echo      scripts\run_detection.bat --source webcam
echo      scripts\run_detection.bat --source video --path data\videos\bus_video.mp4
echo   3. scripts\run_dashboard.bat            (conductor dashboard at http://127.0.0.1:8501)
echo   Demo data: .venv\Scripts\python.exe scripts\generate_dummy_data.py
endlocal
exit /b 0

:wrong_venv
echo Python 3.11 is required, but .venv uses a different version. Delete the .venv folder and run this script again.
exit /b 1
:venv_failed
echo [ERROR] Could not create .venv
exit /b 1
:no_requirements
echo [ERROR] requirements.txt not found in the project root.
exit /b 1
:install_failed
echo [ERROR] Dependency installation failed. See the messages above.
exit /b 1
:db_failed
echo [ERROR] Database initialization failed. See the messages above.
exit /b 1
