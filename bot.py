"""
AS-VISA Hungary Schengen Appointment Bot
Monitors Istanbul and Ankara booking pages and notifies when slots open.
"""

import asyncio
import json
import time
import winsound
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from plyer import notification


def load_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def notify(city, url):
    title = f"Vize Randevusu Acildi! - {city}"
    message = f"{city} icin randevu acildi. Hemen rezervasyon yap!"

    try:
        notification.notify(
            title=title,
            message=message,
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


async def check_url(page, entry, no_appt_phrase):
    """
    Returns True if an appointment slot is available.
    Detection: the 'no quota' phrase is absent from the visible page text.
    """
    url = entry["url"]
    city = entry["name"]

    await page.goto(url, timeout=30_000)

    # Wait for the loading spinner to disappear
    try:
        await page.wait_for_selector(".preloader, #preloader, .loading-overlay", state="hidden", timeout=10_000)
    except Exception:
        pass

    # Wait a bit for JS to render the content
    await asyncio.sleep(2)

    text = await page.evaluate("document.body.innerText")

    if no_appt_phrase in text:
        return False
    else:
        return True


async def run():
    config = load_config()
    urls = config["urls"]
    interval = config["check_interval_seconds"]
    no_appt_phrase = config["no_appointment_phrase"]

    print("Visa Appointment Bot started")
    print(f"Checking every {interval}s for: {', '.join(e['name'] for e in urls)}\n")

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

        check_count = 0

        while True:
            check_count += 1
            now = datetime.now().strftime("%H:%M:%S")
            found_any = False

            for entry in urls:
                try:
                    available = await check_url(page, entry, no_appt_phrase)
                    status = "AVAILABLE !!!" if available else "unavailable"
                    print(f"[{now}] #{check_count} {entry['name']}: {status}")

                    if available:
                        if config.get("screenshot_on_found"):
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            path = Path(f"found_{entry['name']}_{ts}.png")
                            await page.screenshot(path=str(path))
                            print(f"  Screenshot: {path}")

                        notify(entry["name"], entry["url"])
                        found_any = True

                except Exception as e:
                    print(f"[{now}] #{check_count} {entry['name']}: ERROR - {e}")

            if found_any:
                # Keep bot alive so you can still get repeat notifications
                # in case you miss the first one
                pass

            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(run())
