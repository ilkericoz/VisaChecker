"""
AS-VISA Hungary Schengen Appointment Bot — Fast HTTP Mode

Hybrid approach:
  - Playwright boots up, loads the page to get past Cloudflare, then extracts cookies.
  - requests uses those cookies for fast HTTP GET checks (~200ms each).
  - Detection is structural: the booking form (AppointmentTabID +
    __RequestVerificationToken) only renders when slots are open; the
    "no slots" block replaces it otherwise.
  - On detection, CSRF + option values are parsed straight from the HTML
    and TarihGetir is called directly to fetch actual available dates —
    no browser needed on the hot path.
  - On 403 or unknown layout, Playwright re-bootstraps the session.
  - Session is proactively refreshed every SESSION_REFRESH_INTERVAL seconds.
  - On IP ban (connection refused), sends Telegram alert and pauses until /resume.

Telegram commands:
  /screenshot          — take fresh screenshots of both pages and send them
  /status              — show check count, last check time, next check ETA, mode
  /fast                — switch to 30-60s interval
  /normal              — switch back to default interval
  /interval <min> <max> — set custom interval in minutes, e.g. /interval 1 5
  /resume              — resume after IP ban (restart modem first, or auto-resumes after 15 min)
  /compare             — fetch each page via HTTP and Playwright, compare results
  /probe               — blindly POST to TarihGetir with tabId 1-15 to discover valid IDs
  /extract             — load each page in Playwright and scrape form field values (only useful when slots are open)
  /book                — manually run the fast-track booker against the most recent detected slot

Config flags:
  auto_book            — auto-launch fast-track booker on slot detection (default false; flip when ready)
  headed_on_book       — show the booking browser window (default true; required for fast-track captcha)

Entry point — all logic lives in visa/.
"""

import asyncio
from visa.loop import run

if __name__ == "__main__":
    asyncio.run(run())
