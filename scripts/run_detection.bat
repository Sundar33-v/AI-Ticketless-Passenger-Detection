@echo off
REM Run the detection pipeline: video source -> YOLO (person only) -> ByteTrack -> entry line -> SQLite.
REM The source comes from camera.source_type in config\config.yaml unless overridden, e.g.:
REM   scripts\run_detection.bat --source phone [--url http://192.168.1.5:8080/video]
REM   scripts\run_detection.bat --source webcam [--camera-index 1]
REM   scripts\run_detection.bat --source video --path data\videos\bus_video.mp4   (or --choose-file)
REM Press q in the video window to stop. Exit codes: 0 ok, 1 config, 2 video source, 3 database, 4 model, 5 unexpected.
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Run scripts\setup_windows.bat first
    exit /b 1
)
"%PY%" -m app.main %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo [ERROR] Detection exited with code %RC%. See the messages above.
endlocal & exit /b %RC%
