@echo off
echo Launching Chrome with visa profile + remote debugging...
echo.
echo When Chrome opens:
echo   1. Visit https://appointment.as-visa.com/tr/istanbul-bireysel-basvuru
echo   2. Get past the Cloudflare check (just wait/click if prompted)
echo   3. Close Chrome
echo.
echo This only needs to be done once. After that the bot handles everything.
echo.
start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%~dp0chrome_visa_profile" --no-first-run --no-default-browser-check
