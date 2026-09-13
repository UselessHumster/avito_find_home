@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0..\.."
set "AVITO_CHROMEDRIVER=%CD%\runtime\chromedriver-win64\chromedriver.exe"

if not exist "runtime" mkdir "runtime"
if not exist "output" mkdir "output"

:restart
echo [%date% %time%] Starting Avito rental monitor>>"runtime\collector.log"
".venv\Scripts\python.exe" semi_manual_collector.py >>"runtime\collector.log" 2>&1
echo [%date% %time%] Collector stopped; restarting in 60 seconds>>"runtime\collector.log"
timeout /t 60 /nobreak >nul
goto restart
