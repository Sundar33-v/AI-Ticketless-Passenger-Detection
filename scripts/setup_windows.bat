@echo off
REM One-time setup for AI Ticketless Passenger Detection on Windows.
REM Creates .venv, installs dependencies, and generates demo tickets if missing.
setlocal
cd /d "%~dp0.."

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH. Install Python 3.10+ from https://www.python.org/ and tick "Add to PATH".
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ...
    python -m venv .venv || (echo [ERROR] Could not create .venv & exit /b 1)
)
set "PY=.venv\Scripts\python.exe"

"%PY%" -m pip install --upgrade pip || exit /b 1

if exist "requirements.txt" (
    echo Installing from requirements.txt ...
    "%PY%" -m pip install -r requirements.txt || (echo [ERROR] Dependency install failed. & exit /b 1)
) else (
    echo [WARN] requirements.txt not found - installing pinned core packages.
    "%PY%" -m pip install ultralytics==8.4.158 opencv-python==4.13.0.92 lap==0.5.13 numpy==2.4.6 ^
        SQLAlchemy==2.1.1 streamlit==1.64.0 pandas==3.0.3 PyYAML==6.0.3 ^
        qrcode==8.2 pillow==12.2.0 pyzbar==0.1.9 pytest==8.3.3 || (echo [ERROR] Dependency install failed. & exit /b 1)
)

if not exist "data\tickets.json" (
    echo Generating demo tickets and QR codes in data\ ...
    "%PY%" -m app.ticket.generate_test_tickets
)

echo.
echo Setup complete.
echo   1. Start the IP Webcam app on the Android phone (same Wi-Fi/hotspot as this laptop).
echo   2. scripts\run_detection.bat   (camera + detection, default http://192.0.0.4:8080/video)
echo   3. scripts\run_dashboard.bat   (conductor dashboard in the browser)
endlocal
