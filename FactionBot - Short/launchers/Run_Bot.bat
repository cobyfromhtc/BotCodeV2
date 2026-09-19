@echo off
setlocal

title FactionBot - Local Run

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo Create it by running:
    echo   py -3.10 -m venv .venv
    echo   .venv\Scripts\Activate.ps1
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "BOT_FILE=%PROJECT_DIR%\src\bot.py"

if not exist "%BOT_FILE%" (
    echo Bot file not found: %BOT_FILE%
    pause
    exit /b 1
)

echo Starting FactionBot...
"%PYTHON_EXE%" "%BOT_FILE%"
pause
