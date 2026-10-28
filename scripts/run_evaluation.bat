@echo off
REM Evaluate person detection on a labelled test set and store the result for the dashboard.
REM   scripts\run_evaluation.bat images --images data\eval\bus01\images [--labels data\eval\bus01\labels]
REM   scripts\run_evaluation.bat mot --gt data\eval\bus01\gt.txt --pred pred.txt --model models\yolov8n.pt
REM   scripts\run_evaluation.bat import --json data\eval\results.json
REM   scripts\run_evaluation.bat list
REM Exit codes: 0 ok, 1 config/dataset, 2 arguments, 3 database, 4 model, 5 unexpected.
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Run scripts\setup_windows.bat first
    exit /b 1
)
"%PY%" -m app.evaluate %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo [ERROR] Evaluation exited with code %RC%. See the messages above.
endlocal & exit /b %RC%
