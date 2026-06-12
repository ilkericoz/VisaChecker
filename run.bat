@echo off
REM Launch the visa bot using the project's venv interpreter — NOT system Python.
REM System Python (the Windows Store python.exe on PATH) lacks ddddocr / capsolver /
REM curl_cffi, so running the bot with bare `python` silently breaks the CAPTCHA
REM solver and the Turnstile fallback. Always start it through this script.
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [run.bat] venv not found at venv\Scripts\python.exe
    echo Create it first:  python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo [run.bat] Starting visa bot with venv interpreter...
"venv\Scripts\python.exe" -m visa
pause
