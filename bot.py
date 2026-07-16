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
  - With auto_book enabled, slot detection launches the fast-track booker:
    real Chrome via CDP (Cloudflare bypass), auto-fill from
    booking_profile.json, Turnstile/CAPTCHA solving, auto-submit.
  - On 403 or unknown layout, Playwright re-bootstraps the session.
  - Session is proactively refreshed every SESSION_REFRESH_INTERVAL seconds.
  - Connection errors are treated as transient — retried next cycle.

Telegram commands:
  /screenshot          — take fresh screenshots of both pages and send them
  /status              — show check count, last check time, next check ETA, mode
  /fast                — switch to 30-60s interval
  /normal              — switch back to default interval
  /interval <min> <max> — set custom interval in minutes, e.g. /interval 1 5
  /book                — manually run the fast-track booker against the most recent detected slot
  (research-era commands still wired up but rarely needed: /probe /compare /extract /resume)

Config flags:
  auto_book            — auto-launch fast-track booker on slot detection (currently enabled)
  headed_on_book       — show the booking browser window (default true; required for fast-track captcha)

Entry point — all logic lives in visa/.
"""

import asyncio
from visa.loop import run

if __name__ == "__main__":
    asyncio.run(run())
