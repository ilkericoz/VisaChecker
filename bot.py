"""
AS-VISA Hungary Schengen Appointment Bot
Monitors Istanbul and Ankara booking pages and notifies when slots open.
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
# A phrase that must always be present on a healthy page load.
# If it's missing the page failed to load properly.
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


def notify(city, url, screenshot_path=None):
    send_telegram(f"RANDEVU ACILDI — {city}\n{url}")
    if screenshot_path:
        send_telegram_photo(screenshot_path, caption=f"{city} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

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


def save_daily_snapshot(city, html):
    """Save one HTML snapshot per city per day for structure change tracking."""
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = SNAPSHOTS_DIR / f"{city}_{date.today()}.html"
    if not path.exists():
        path.write_text(html, encoding="utf-8")
        print(f"  Snapshot saved: {path}")


async def check_url(page, entry, no_appt_phrase):
    """
    Returns (available, status_message).
    Raises if the page fails the sanity check.
    """
    await page.goto(entry["url"], timeout=30_000)
    try:
        await page.wait_for_selector(".preloader, #preloader, .loading-overlay", state="hidden", timeout=10_000)
    except Exception:
        pass

    # Dismiss the 'Onemli Bilgilendirme' popup
    await page.keyboard.press("Escape")
    await asyncio.sleep(2)

    text = await page.evaluate("document.body.innerText")
    html = await page.content()

    # Sanity check — make sure the page actually loaded
    if SANITY_PHRASE not in text:
        raise RuntimeError(f"Sanity check failed: '{SANITY_PHRASE}' not found — page may not have loaded correctly")

    save_daily_snapshot(entry["name"], html)

    return no_appt_phrase not in text, html


async def run():
    config = load_config()
    urls = config["urls"]
    interval_min = config["check_interval_min_seconds"]
    interval_max = config["check_interval_max_seconds"]
    no_appt_phrase = config["no_appointment_phrase"]

    print("Visa Appointment Bot started")
    print(f"Checking every {interval_min//60}-{interval_max//60} min for: {', '.join(e['name'] for e in urls)}\n")

    send_telegram(f"Argos started. Checking {', '.join(e['name'] for e in urls)} every {interval_min//60}-{interval_max//60} min.")

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

        check_count = 0
        last_heartbeat = time.time()

        while True:
            check_count += 1
            now = datetime.now().strftime("%H:%M:%S")

            for entry in urls:
                try:
                    available, html = await check_url(page, entry, no_appt_phrase)
                    status = "AVAILABLE !!!" if available else "unavailable"
                    print(f"[{now}] #{check_count} {entry['name']}: {status}")

                    if available:
                        screenshot_path = None
                        if config.get("screenshot_on_found"):
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            base = f"found_{entry['name']}_{ts}"
                            screenshot_path = f"{base}.png"
                            await page.screenshot(path=screenshot_path)
                            print(f"  Screenshot: {screenshot_path}")
                            Path(f"{base}.html").write_text(html, encoding="utf-8")
                            print(f"  HTML saved:  {base}.html")

                        notify(entry["name"], entry["url"], screenshot_path)

                except Exception as e:
                    print(f"[{now}] #{check_count} {entry['name']}: ERROR - {e}")
                    send_telegram(f"Argos error on {entry['name']}: {e}")

            # Heartbeat
            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_telegram(f"Argos still running. {check_count} checks done, no slots yet.")
                last_heartbeat = time.time()

            wait = random.uniform(interval_min, interval_max)
            print(f"  Next check in {wait/60:.1f} min...")
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(run())
