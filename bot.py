"""
AS-VISA Hungary Schengen Appointment Bot — Fast HTTP Mode

Hybrid approach:
  - Playwright boots up, loads the page to get past Cloudflare, then extracts cookies.
  - requests uses those cookies for fast HTTP GET checks (~200ms each).
  - On 403 or sanity failure, Playwright re-bootstraps the session.
  - Session is proactively refreshed every SESSION_REFRESH_INTERVAL seconds.
  - On IP ban (connection refused), sends Telegram alert and pauses until /resume.

Telegram commands:
  /screenshot          — take fresh screenshots of both pages and send them
  /status              — show check count, last check time, next check ETA, mode
  /fast                — switch to 30-60s interval
  /normal              — switch back to default interval
  /interval <min> <max> — set custom interval in minutes, e.g. /interval 1 5
  /resume              — resume after IP ban (restart modem first)
"""

import asyncio
import json
import os
import random
import time
import winsound
from datetime import datetime, date
from pathlib import Path

import requests as req_lib
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from plyer import notification

load_dotenv()

HEARTBEAT_INTERVAL = 6 * 60 * 60      # 6 hours
SESSION_REFRESH_INTERVAL = 15 * 60    # re-bootstrap Playwright session every 15 min
SNAPSHOTS_DIR = Path("snapshots")
SANITY_PHRASE = "Randevu"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class IPBannedError(Exception):
    pass


def load_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_telegram(message):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        req_lib.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[!] Telegram message failed: {e}")


def send_telegram_photo(path, caption=""):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        with open(path, "rb") as f:
            req_lib.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=20,
            )
    except Exception as e:
        print(f"[!] Telegram photo failed: {e}")


def get_telegram_updates(offset):
    token = os.environ["TELEGRAM_TOKEN"]
    try:
        r = req_lib.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        return r.json().get("result", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

def notify(city, url):
    send_telegram(f"RANDEVU ACILDI — {city}\n{url}")

    try:
        notification.notify(
            title=f"Vize Randevusu Acildi! - {city}",
            message=f"{city} icin randevu acildi. Hemen rezervasyon yap!",
            app_name="Visa Bot",
            timeout=30,
        )
    except Exception as e:
        print(f"[!] Desktop notification failed: {e}")

    for _ in range(10):
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        time.sleep(0.3)

    print(f"\n{'='*60}")
    print(f"  RANDEVU ACILDI! - {city}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {url}")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def save_daily_snapshot(city, html):
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = SNAPSHOTS_DIR / f"{city}_{date.today()}.html"
    if not path.exists():
        path.write_text(html, encoding="utf-8")
        print(f"  Snapshot saved: {path}")


async def take_and_send_screenshot(page, city, html):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"found_{city}_{ts}"
        await page.screenshot(path=f"{base}.png", full_page=True)
        Path(f"{base}.html").write_text(html, encoding="utf-8")
        print(f"  Screenshot: {base}.png  HTML: {base}.html")
        send_telegram_photo(f"{base}.png", caption=f"{city} - {ts}")
    except Exception as e:
        print(f"[!] Screenshot/send failed: {e}")


# ---------------------------------------------------------------------------
# Session management — Playwright bootstraps, requests checks fast
# ---------------------------------------------------------------------------

async def bootstrap_session(page, urls):
    """
    Load each URL in Playwright to get past Cloudflare, then extract cookies.
    Returns a requests.Session ready to use with those cookies.
    """
    print("  [Session] Bootstrapping session via Playwright...")
    http_session = req_lib.Session()
    http_session.headers.update({"User-Agent": USER_AGENT})

    for entry in urls:
        try:
            await page.goto(entry["url"], timeout=30_000)
            try:
                await page.wait_for_selector(
                    ".preloader, #preloader, .loading-overlay",
                    state="hidden", timeout=10_000,
                )
            except Exception:
                pass
            # Wait for auto-closing popup
            await asyncio.sleep(5)

            # Extract cookies from Playwright context and put them in requests session
            cookies = await page.context.cookies()
            for c in cookies:
                http_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        except Exception as e:
            print(f"  [Session] Bootstrap failed for {entry['name']}: {e}")

    print(f"  [Session] Got {len(http_session.cookies)} cookies from Playwright")
    return http_session


def fast_check(http_session, entry, no_appt_phrase):
    """
    Fast HTTP GET check using the shared requests session.
    Returns (available: bool, html: str, elapsed: float) or raises on failure.
    Raises IPBannedError on connection refused (Cloudflare IP ban).
    """
    t0 = time.time()
    try:
        r = http_session.get(entry["url"], timeout=15)
    except req_lib.exceptions.ConnectionError as e:
        raise IPBannedError(f"Connection refused — IP likely banned") from e

    elapsed = time.time() - t0

    if r.status_code == 403:
        raise RuntimeError("403 — session expired, need Playwright refresh")

    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")

    html = r.text

    if SANITY_PHRASE not in html:
        raise RuntimeError(f"Sanity check failed: '{SANITY_PHRASE}' not found")

    available = no_appt_phrase not in html
    return available, html, elapsed


# ---------------------------------------------------------------------------
# Telegram command listener
# ---------------------------------------------------------------------------

async def telegram_command_listener(state, page_lock, page, config):
    urls = config["urls"]
    default_min = config["check_interval_min_seconds"]
    default_max = config["check_interval_max_seconds"]
    offset = 0

    while True:
        updates = await asyncio.get_event_loop().run_in_executor(
            None, get_telegram_updates, offset
        )

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) != os.environ["TELEGRAM_CHAT_ID"]:
                continue
            raw = msg.get("text", "").strip()
            cmd = raw.lower()

            if cmd == "/screenshot":
                print("[CMD] /screenshot")
                send_telegram("Taking screenshots, one moment...")
                async with page_lock:
                    for entry in urls:
                        try:
                            await page.goto(entry["url"], timeout=30_000)
                            try:
                                await page.wait_for_selector(
                                    ".preloader, #preloader, .loading-overlay",
                                    state="hidden", timeout=10_000,
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(5)
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            path = f"cmd_screenshot_{entry['name']}_{ts}.png"
                            await page.screenshot(path=path, full_page=True)
                            send_telegram_photo(path, caption=entry["name"])
                        except Exception as e:
                            send_telegram(f"Screenshot failed for {entry['name']}: {e}")

            elif cmd == "/status":
                print("[CMD] /status")
                imin, imax = state["interval_min"], state["interval_max"]
                last = state["last_check_time"] or "not yet"
                next_in = state["next_check_in"]
                next_str = f"{next_in:.1f} min" if next_in is not None else "soon"
                fast_count = state["fast_check_count"]
                pw_count = state["playwright_check_count"]
                last_refresh = state["last_session_refresh"] or "not yet"
                banned = " ⚠️ IP BANNED — send /resume after modem restart" if state["ip_banned"] else ""
                send_telegram(
                    f"Argos status (fast HTTP mode){banned}\n"
                    f"Fast checks: {fast_count}  Playwright checks: {pw_count}\n"
                    f"Last check: {last}\n"
                    f"Next check in: {next_str}\n"
                    f"Interval: {imin//60}-{imax//60} min\n"
                    f"Last session refresh: {last_refresh}"
                )

            elif cmd == "/fast":
                print("[CMD] /fast")
                state["interval_min"] = 30
                state["interval_max"] = 60
                send_telegram("Switched to fast mode: checking every 30-60s.")

            elif cmd == "/normal":
                print("[CMD] /normal")
                state["interval_min"] = default_min
                state["interval_max"] = default_max
                send_telegram(
                    f"Switched to normal mode: checking every "
                    f"{default_min//60}-{default_max//60} min."
                )

            elif cmd == "/resume":
                print("[CMD] /resume")
                if state["ip_banned"]:
                    state["ip_banned"] = False
                    send_telegram("Resuming checks. Good luck!")
                else:
                    send_telegram("Not paused.")

            elif cmd.startswith("/interval"):
                print(f"[CMD] /interval: {raw}")
                try:
                    parts = raw.split()
                    imin = int(parts[1]) * 60
                    imax = int(parts[2]) * 60
                    if imin < 30:
                        send_telegram("Minimum interval is 0.5 min (30s).")
                    elif imin >= imax:
                        send_telegram("First value must be less than second.")
                    else:
                        state["interval_min"] = imin
                        state["interval_max"] = imax
                        send_telegram(f"Interval set to {imin//60}-{imax//60} min.")
                except (IndexError, ValueError):
                    send_telegram("Usage: /interval <min> <max> (minutes)\nExample: /interval 1 5")

        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run():
    config = load_config()
    urls = config["urls"]
    no_appt_phrase = config["no_appointment_phrase"]

    print("Visa Appointment Bot — Fast HTTP Mode")
    print(f"Checking every {config['check_interval_min_seconds']//60}-{config['check_interval_max_seconds']//60} min")
    print(f"Cities: {', '.join(e['name'] for e in urls)}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config["headless"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page_lock = asyncio.Lock()

        # Bootstrap initial session
        async with page_lock:
            http_session = await bootstrap_session(page, urls)
        last_session_refresh = time.time()

        state = {
            "fast_check_count": 0,
            "playwright_check_count": 0,
            "last_check_time": None,
            "next_check_in": None,
            "interval_min": config["check_interval_min_seconds"],
            "interval_max": config["check_interval_max_seconds"],
            "last_session_refresh": datetime.now().strftime("%H:%M:%S"),
            "ip_banned": False,
        }

        send_telegram(
            f"Argos started (fast HTTP mode).\n"
            f"Watching {', '.join(e['name'] for e in urls)} every "
            f"{state['interval_min']//60}-{state['interval_max']//60} min.\n"
            f"Commands: /screenshot, /status, /fast, /normal, /interval"
        )

        asyncio.create_task(telegram_command_listener(state, page_lock, page, config))

        last_heartbeat = time.time()

        while True:
            now = datetime.now().strftime("%H:%M:%S")

            # Pause loop while IP banned — wait for /resume
            if state["ip_banned"]:
                print(f"[{now}] IP banned — paused. Send /resume after modem restart.")
                await asyncio.sleep(30)
                continue

            # Proactive session refresh every SESSION_REFRESH_INTERVAL
            if time.time() - last_session_refresh >= SESSION_REFRESH_INTERVAL:
                print(f"[{now}] Session refresh (proactive)...")
                async with page_lock:
                    http_session = await bootstrap_session(page, urls)
                last_session_refresh = time.time()
                state["last_session_refresh"] = now

            loop = asyncio.get_event_loop()
            for entry in urls:
                try:
                    # Fast HTTP check — run in executor to avoid blocking event loop
                    available, html, elapsed = await loop.run_in_executor(
                        None, fast_check, http_session, entry, no_appt_phrase
                    )
                    state["fast_check_count"] += 1
                    status = "AVAILABLE !!!" if available else "unavailable"
                    print(f"[{now}] HTTP #{state['fast_check_count']} {entry['name']}: {status} ({elapsed*1000:.0f}ms)")

                    save_daily_snapshot(entry["name"], html)

                    if available:
                        notify(entry["name"], entry["url"])
                        if config.get("screenshot_on_found"):
                            async with page_lock:
                                await take_and_send_screenshot(page, entry["name"], html)

                except IPBannedError as e:
                    print(f"[{now}] {e}")
                    if not state["ip_banned"]:
                        state["ip_banned"] = True
                        send_telegram(
                            "IP banned by Cloudflare — checks paused.\n"
                            "Restart your modem, then send /resume."
                        )
                    break  # no point checking other cities

                except Exception as e:
                    print(f"[{now}] HTTP check failed ({entry['name']}): {e} — falling back to Playwright")

                    # Fall back to full Playwright load and re-bootstrap session
                    try:
                        async with page_lock:
                            await page.goto(entry["url"], timeout=30_000)
                            try:
                                await page.wait_for_selector(
                                    ".preloader, #preloader, .loading-overlay",
                                    state="hidden", timeout=10_000,
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(5)

                            text = await page.evaluate("document.body.innerText")
                            html = await page.content()

                            if SANITY_PHRASE not in text:
                                raise RuntimeError("Sanity check failed after Playwright fallback")

                            available = no_appt_phrase not in text
                            state["playwright_check_count"] += 1
                            status = "AVAILABLE !!!" if available else "unavailable"
                            print(f"[{now}] PW  #{state['playwright_check_count']} {entry['name']}: {status}")

                            save_daily_snapshot(entry["name"], html)

                            if available:
                                notify(entry["name"], entry["url"])
                                if config.get("screenshot_on_found"):
                                    await take_and_send_screenshot(page, entry["name"], html)

                            # Refresh cookies from this successful Playwright load
                            cookies = await page.context.cookies()
                            for c in cookies:
                                http_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
                            last_session_refresh = time.time()
                            state["last_session_refresh"] = now
                            print("  [Session] Cookies refreshed after fallback")

                    except Exception as e2:
                        if "ERR_CONNECTION_REFUSED" in str(e2) or "Connection refused" in str(e2):
                            if not state["ip_banned"]:
                                state["ip_banned"] = True
                                send_telegram(
                                    "IP banned by Cloudflare — checks paused.\n"
                                    "Restart your modem, then send /resume."
                                )
                            break
                        print(f"[{now}] Playwright fallback also failed ({entry['name']}): {e2}")
                        send_telegram(f"Argos error on {entry['name']}: {e2}")

            state["last_check_time"] = now

            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                total = state["fast_check_count"] + state["playwright_check_count"]
                send_telegram(
                    f"Argos still running (fast HTTP). "
                    f"{total} total checks ({state['fast_check_count']} fast, "
                    f"{state['playwright_check_count']} Playwright). No slots yet."
                )
                last_heartbeat = time.time()

            wait = random.uniform(state["interval_min"], state["interval_max"])
            state["next_check_in"] = wait / 60
            print(f"  Next check in {wait/60:.1f} min...")
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(run())
