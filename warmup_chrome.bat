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
REM These flags MUST match visa\browser.py launch_chrome_cdp(). If they don't,
REM the bot attaches to a Chrome that still reports navigator.webdriver=true and
REM Cloudflare Turnstile detects automation -> cfToken never gets set.
start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%~dp0chrome_visa_profile" --no-first-run --no-default-browser-check --disable-blink-features=AutomationControlled --excludeSwitches=enable-automation --disable-infobars
