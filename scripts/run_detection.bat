@echo off
REM Run the detection pipeline: phone camera -> YOLO (person only) -> ByteTrack -> entry line -> SQLite.
REM Extra arguments are passed through, e.g.:
REM   scripts\run_detection.bat --source 0
REM   scripts\run_detection.bat --source "http://192.168.1.5:8080/video" --no-display
setlocal
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo Camera source: config\config.yaml (default http://192.0.0.4:8080/video). Press q in the video window to stop.
"%PY%" -m app.main %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo [ERROR] Detection exited with code %RC%. See messages above.
endlocal & exit /b %RC%
