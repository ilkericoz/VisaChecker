"""
AS-VISA Hungary Schengen Appointment Bot
Monitors Istanbul and Ankara booking pages and notifies when slots open.

Telegram commands:
  /screenshot          — take fresh screenshots of both pages and send them
  /status              — show check count, last check time, next check ETA
  /fast                — switch to 1-3 min interval
  /normal              — switch back to default 3-10 min interval
  /interval <min> <max> — set custom interval in minutes, e.g. /interval 5 15
"""

import asyncio
import json
import os
import random
import time
import winsound
from datetime import datetime, date
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from plyer import notification

load_dotenv()

HEARTBEAT_INTERVAL = 6 * 60 * 60  # 6 hours
SNAPSHOTS_DIR = Path("snapshots")
SANITY_PHRASE = "Randevu"


def load_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def send_telegram(message):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        requests.post(
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
            requests.post(
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
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        return r.json().get("result", [])
    except Exception:
        return []


def notify(city, url):
    """Fire the immediate alert — text message, beeps, desktop notification."""
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


async def take_and_send_screenshot(page, city, html):
    """Take a full-page screenshot and send it — runs in background after alert fires."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"found_{city}_{ts}"
        await page.screenshot(path=f"{base}.png", full_page=True)
        Path(f"{base}.html").write_text(html, encoding="utf-8")
        print(f"  Screenshot: {base}.png  HTML: {base}.html")
        send_telegram_photo(f"{base}.png", caption=f"{city} - {ts}")
    except Exception as e:
        print(f"[!] Screenshot/send failed: {e}")


def save_daily_snapshot(city, html):
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = SNAPSHOTS_DIR / f"{city}_{date.today()}.html"
    if not path.exists():
        path.write_text(html, encoding="utf-8")
        print(f"  Snapshot saved: {path}")


async def check_url(page, entry, no_appt_phrase):
    await page.goto(entry["url"], timeout=30_000)
    try:
        await page.wait_for_selector(".preloader, #preloader, .loading-overlay", state="hidden", timeout=10_000)
    except Exception:
        pass
    # Wait for the auto-closing 'Onemli Bilgilendirme' popup to finish its countdown
    await asyncio.sleep(5)

    text = await page.evaluate("document.body.innerText")
    html = await page.content()

    if SANITY_PHRASE not in text:
        raise RuntimeError(f"Sanity check failed: '{SANITY_PHRASE}' not found — page may not have loaded correctly")

    save_daily_snapshot(entry["name"], html)
    return no_appt_phrase not in text, html


async def telegram_command_listener(state, page_lock, config, page):
    """Polls Telegram for commands and responds."""
    urls = config["urls"]
    default_min = config["check_interval_min_seconds"]
    default_max = config["check_interval_max_seconds"]
    offset = 0

    while True:
        updates = await asyncio.get_event_loop().run_in_executor(None, get_telegram_updates, offset)

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            # Only respond to your own chat
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
                                await page.wait_for_selector(".preloader, #preloader, .loading-overlay", state="hidden", timeout=10_000)
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
                send_telegram(
                    f"Argos status\n"
                    f"Checks done: {state['check_count']}\n"
                    f"Last check: {last}\n"
                    f"Next check in: {next_str}\n"
                    f"Interval: {imin//60}-{imax//60} min"
                )

            elif cmd == "/fast":
                print("[CMD] /fast")
                state["interval_min"] = 60
                state["interval_max"] = 180
                send_telegram("Switched to fast mode: checking every 1-3 min.")

            elif cmd == "/normal":
                print("[CMD] /normal")
                state["interval_min"] = default_min
                state["interval_max"] = default_max
                send_telegram(f"Switched to normal mode: checking every {default_min//60}-{default_max//60} min.")

            elif cmd.startswith("/interval"):
                print(f"[CMD] /interval: {raw}")
                try:
                    parts = raw.split()
                    imin = int(parts[1]) * 60
                    imax = int(parts[2]) * 60
                    if imin < 30:
                        send_telegram("Minimum interval is 0.5 min (30s). Use /interval 1 5 for 1-5 min.")
                    elif imin >= imax:
                        send_telegram("First value must be less than second. Example: /interval 2 8")
                    else:
                        state["interval_min"] = imin
                        state["interval_max"] = imax
                        send_telegram(f"Interval set to {imin//60}-{imax//60} min.")
                except (IndexError, ValueError):
                    send_telegram("Usage: /interval <min> <max> (in minutes)\nExample: /interval 5 15")

        await asyncio.sleep(3)


async def run():
    config = load_config()
    urls = config["urls"]
    no_appt_phrase = config["no_appointment_phrase"]

    print("Visa Appointment Bot started")
    print("Commands: /screenshot, /status, /fast, /normal, /interval <min> <max>")
    print(f"Checking every {config['check_interval_min_seconds']//60}-{config['check_interval_max_seconds']//60} min for: {', '.join(e['name'] for e in urls)}\n")

    send_telegram(
        f"Argos started. Checking {', '.join(e['name'] for e in urls)} every "
        f"{config['check_interval_min_seconds']//60}-{config['check_interval_max_seconds']//60} min.\n"
        f"Commands: /screenshot, /status, /fast, /normal, /interval <min> <max>"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config["headless"])
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        state = {
            "check_count": 0,
            "last_check_time": None,
            "next_check_in": None,
            "interval_min": config["check_interval_min_seconds"],
            "interval_max": config["check_interval_max_seconds"],
        }
        page_lock = asyncio.Lock()

        asyncio.create_task(telegram_command_listener(state, page_lock, config, page))

        last_heartbeat = time.time()

        while True:
            state["check_count"] += 1
            now = datetime.now().strftime("%H:%M:%S")

            async with page_lock:
                for entry in urls:
                    try:
                        available, html = await check_url(page, entry, no_appt_phrase)
                        status = "AVAILABLE !!!" if available else "unavailable"
                        print(f"[{now}] #{state['check_count']} {entry['name']}: {status}")

                        if available:
                            notify(entry["name"], entry["url"])
                            if config.get("screenshot_on_found"):
                                asyncio.create_task(take_and_send_screenshot(page, entry["name"], html))

                    except Exception as e:
                        print(f"[{now}] #{state['check_count']} {entry['name']}: ERROR - {e}")
                        send_telegram(f"Argos error on {entry['name']}: {e}")

            state["last_check_time"] = now

            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_telegram(f"Argos still running. {state['check_count']} checks done, no slots yet.")
                last_heartbeat = time.time()

            wait = random.uniform(state["interval_min"], state["interval_max"])
            state["next_check_in"] = wait / 60
            print(f"  Next check in {wait/60:.1f} min...")
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(run())
