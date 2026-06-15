@echo off
cd /d "%~dp0"
title Visa Bot

echo ============================================================
echo  Visa Appointment Bot
echo ============================================================
echo.

REM --- venv check ---
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found. Run this once to set it up:
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM --- booking profile check ---
if not exist "booking_profile.json" (
    echo [ERROR] booking_profile.json not found.
    echo Copy booking_profile.example.json to booking_profile.json and fill in your details.
    echo.
    pause
    exit /b 1
)

REM --- warn if still using placeholder data ---
findstr /i "Ahmet Yilmaz" booking_profile.json >nul 2>&1
if %errorlevel%==0 (
    echo [WARNING] booking_profile.json still has placeholder data!
    echo           The bot will try to book with FAKE personal info.
    echo           Update the file with your real details before a real booking.
    echo.
    echo Press any key to run anyway, or close this window to cancel.
    pause >nul
)

echo [OK] Launching bot...
echo.
"venv\Scripts\python.exe" -m visa
echo.
echo Bot stopped.
pause
