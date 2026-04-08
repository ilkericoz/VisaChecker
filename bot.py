"""
AS-VISA Hungary Schengen Appointment Bot — Human Browser Mode

Connects to your real Chrome browser via CDP (Chrome DevTools Protocol).
Opens each URL in a real tab and periodically checks the DOM for changes.
Looks exactly like a human browsing — real cookies, history, fingerprint.

Setup:
  1. Close all Chrome windows
  2. Launch Chrome with:
     chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeBot
  3. Run this bot: python bot.py

The bot will open tabs in your Chrome and refresh them to check for changes.
Between refreshes it simulates human-like behavior (random scroll, mouse moves).

Telegram commands:
  /screenshot          — take screenshots of all watched tabs
  /status              — show check count, last check time, next check ETA
  /fast                — switch to 2-4 min interval
  /normal              — switch back to default interval
  /interval <min> <max> — set custom interval in minutes, e.g. /interval 3 8
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

HEARTBEAT_INTERVAL = 6 * 60 * 60  # 6 hours
SNAPSHOTS_DIR = Path("snapshots")
CDP_URL = "http://localhost:9222"


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


async def take_and_send_screenshot(page, city):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"found_{city}_{ts}.png"
        await page.screenshot(path=path, full_page=True)
        print(f"  Screenshot: {path}")
        send_telegram_photo(path, caption=f"{city} - {ts}")
    except Exception as e:
        print(f"[!] Screenshot failed: {e}")


# ---------------------------------------------------------------------------
# Human-like behavior — make the browser activity look natural
# ---------------------------------------------------------------------------

async def human_idle(page):
    """Simulate idle human behavior on the page — small scrolls, mouse moves."""
    try:
        viewport = await page.evaluate("({ w: window.innerWidth, h: window.innerHeight })")
        w, h = viewport["w"], viewport["h"]

        # Random mouse move
        x = random.randint(100, max(101, w - 100))
        y = random.randint(100, max(101, h - 100))
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.3, 1.0))

        # Small scroll
        scroll_amount = random.randint(-150, 150)
        await page.mouse.wheel(0, scroll_amount)
        await asyncio.sleep(random.uniform(0.2, 0.6))

        # Maybe another mouse move
        if random.random() < 0.5:
            x2 = random.randint(100, max(101, w - 100))
            y2 = random.randint(100, max(101, h - 100))
            await page.mouse.move(x2, y2)
    except Exception:
        pass


async def human_refresh(page):
    """Refresh the page like a human would — F5 or click refresh, not page.reload()."""
    method = random.choice(["f5", "ctrl_r", "reload"])
    try:
        if method == "f5":
            await page.keyboard.press("F5")
        elif method == "ctrl_r":
            await page.keyboard.down("Control")
            await page.keyboard.press("r")
            await page.keyboard.up("Control")
        else:
            await page.reload()

        # Wait for page to load
        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_selector(
                ".preloader, #preloader, .loading-overlay",
                state="hidden", timeout=10_000,
            )
        except Exception:
            pass
    except Exception as e:
        # Fallback to regular reload
        try:
            await page.reload(timeout=30_000)
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            raise RuntimeError(f"Page refresh failed: {e}")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

async def check_page(page, entry, no_appt_phrase):
    """Check if the page shows available appointments."""
    text = await page.evaluate("document.body.innerText")
    html = await page.content()

    sanity_phrase = "Randevu"
    if sanity_phrase not in text:
        raise RuntimeError(f"Sanity check failed: '{sanity_phrase}' not in page text")

    save_daily_snapshot(entry["name"], html)
    available = no_appt_phrase not in text
    return available, html


# ---------------------------------------------------------------------------
# Telegram command listener
# ---------------------------------------------------------------------------

async def telegram_command_listener(state, pages, config):
    """Polls Telegram for commands and responds."""
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
                send_telegram("Taking screenshots...")
                for name, page in pages.items():
                    try:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        path = f"cmd_screenshot_{name}_{ts}.png"
                        await page.screenshot(path=path, full_page=True)
                        send_telegram_photo(path, caption=name)
                    except Exception as e:
                        send_telegram(f"Screenshot failed for {name}: {e}")

            elif cmd == "/status":
                print("[CMD] /status")
                imin, imax = state["interval_min"], state["interval_max"]
                last = state["last_check_time"] or "not yet"
                next_in = state["next_check_in"]
                next_str = f"{next_in:.1f} min" if next_in is not None else "soon"
                send_telegram(
                    f"Argos status (human browser mode)\n"
                    f"Checks done: {state['check_count']}\n"
                    f"Last check: {last}\n"
                    f"Next check in: {next_str}\n"
                    f"Interval: {imin/60:.1f}-{imax/60:.1f} min\n"
                    f"Tabs open: {len(pages)}"
                )

            elif cmd == "/fast":
                print("[CMD] /fast")
                state["interval_min"] = 120
                state["interval_max"] = 240
                send_telegram("Switched to fast mode: refreshing every 2-4 min.")

            elif cmd == "/normal":
                print("[CMD] /normal")
                state["interval_min"] = default_min
                state["interval_max"] = default_max
                send_telegram(
                    f"Switched to normal mode: refreshing every "
                    f"{default_min/60:.1f}-{default_max/60:.1f} min."
                )

            elif cmd.startswith("/interval"):
                print(f"[CMD] /interval: {raw}")
                try:
                    parts = raw.split()
                    imin = float(parts[1]) * 60
                    imax = float(parts[2]) * 60
                    if imin < 60:
                        send_telegram("Minimum interval is 1 min for human mode.")
                    elif imin >= imax:
                        send_telegram("First value must be less than second.")
                    else:
                        state["interval_min"] = imin
                        state["interval_max"] = imax
                        send_telegram(f"Interval set to {imin/60:.1f}-{imax/60:.1f} min.")
                except (IndexError, ValueError):
                    send_telegram(
                        "Usage: /interval <min> <max> (in minutes)\n"
                        "Example: /interval 3 8"
                    )

        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run():
    config = load_config()
    urls = config["urls"]
    no_appt_phrase = config["no_appointment_phrase"]

    print("Visa Appointment Bot — Human Browser Mode")
    print(f"Connecting to Chrome at {CDP_URL}...")
    print("Make sure Chrome is running with: chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeBot\n")

    async with async_playwright() as p:
        # Connect to the user's real Chrome
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"Failed to connect to Chrome at {CDP_URL}")
            print(f"Error: {e}")
            print("\nLaunch Chrome first with:")
            print('  chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeBot')
            return

        print(f"Connected to Chrome ({len(browser.contexts)} existing context(s))")

        # Use existing context or create one
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context()

        # Open a tab for each URL
        pages = {}  # name -> page
        for entry in urls:
            page = await context.new_page()
            print(f"Opening {entry['name']}: {entry['url']}")
            await page.goto(entry["url"], timeout=30_000)
            try:
                await page.wait_for_selector(
                    ".preloader, #preloader, .loading-overlay",
                    state="hidden", timeout=10_000,
                )
            except Exception:
                pass
            # Wait for any popup to close
            await asyncio.sleep(6)
            pages[entry["name"]] = page
            # Small delay between tab opens — human pace
            await asyncio.sleep(random.uniform(1, 3))

        print(f"\n{len(pages)} tabs open. Starting monitoring.\n")

        # Build entry lookup
        entry_by_name = {e["name"]: e for e in urls}

        state = {
            "check_count": 0,
            "last_check_time": None,
            "next_check_in": None,
            "interval_min": config["check_interval_min_seconds"],
            "interval_max": config["check_interval_max_seconds"],
        }

        send_telegram(
            f"Argos started (human browser mode).\n"
            f"Watching {', '.join(pages.keys())} in real Chrome tabs.\n"
            f"Refreshing every {state['interval_min']/60:.1f}-"
            f"{state['interval_max']/60:.1f} min.\n"
            f"Commands: /screenshot, /status, /fast, /normal, /interval"
        )

        asyncio.create_task(telegram_command_listener(state, pages, config))

        last_heartbeat = time.time()

        while True:
            state["check_count"] += 1
            now = datetime.now().strftime("%H:%M:%S")

            # Check each tab
            for name, page in pages.items():
                entry = entry_by_name[name]
                try:
                    # Bring tab to front briefly (like a human switching tabs)
                    await page.bring_to_front()
                    await asyncio.sleep(random.uniform(0.5, 1.5))

                    # Do some human-like idle activity before refreshing
                    await human_idle(page)
                    await asyncio.sleep(random.uniform(0.5, 2.0))

                    # Refresh the page like a human
                    await human_refresh(page)

                    # Wait for popup to auto-close
                    await asyncio.sleep(random.uniform(5, 7))

                    # Check for availability
                    available, html = await check_page(page, entry, no_appt_phrase)
                    status = "AVAILABLE !!!" if available else "unavailable"
                    print(f"[{now}] #{state['check_count']} {name}: {status}")

                    if available:
                        notify(name, entry["url"])
                        await take_and_send_screenshot(page, name)

                except Exception as e:
                    print(f"[{now}] #{state['check_count']} {name}: ERROR - {e}")
                    send_telegram(f"Argos error on {name}: {e}")

                    # Try to recover the tab
                    try:
                        await page.goto(entry["url"], timeout=30_000)
                        await asyncio.sleep(6)
                    except Exception:
                        pass

                # Random delay between tabs — like a human switching
                if len(pages) > 1:
                    await asyncio.sleep(random.uniform(2, 5))

            state["last_check_time"] = now

            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_telegram(
                    f"Argos still running (human browser). "
                    f"{state['check_count']} checks done, no slots yet."
                )
                last_heartbeat = time.time()

            wait = random.uniform(state["interval_min"], state["interval_max"])
            state["next_check_in"] = wait / 60
            print(f"  Next check in {wait/60:.1f} min...")
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(run())
