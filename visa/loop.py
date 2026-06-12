import asyncio
import random
import time
from datetime import datetime

from playwright.async_api import async_playwright

from visa.config import (
    HEARTBEAT_INTERVAL, SESSION_REFRESH_INTERVAL, IP_BAN_AUTO_RESUME_AFTER,
    SANITY_PHRASE, FORM_MARKER, CSRF_MARKER,
    USER_AGENT, BOOKING_PROFILE_PATH,
    IPBannedError, load_config, load_booking_profile,
)
from visa.telegram import send_telegram, telegram_command_listener
from visa.detector import (
    fast_check, bootstrap_session, save_daily_snapshot,
    notify, take_and_send_screenshot, report_dates_from_html,
)
from visa.forensics import dump_forensic_bundle
from visa.booker import fast_track_book


async def run():
    config = load_config()
    urls = config["urls"]
    no_appt_phrase = config["no_appointment_phrase"]

    print("Visa Appointment Bot — Fast HTTP Mode")
    print(f"Checking every {config['check_interval_min_seconds']//60}-{config['check_interval_max_seconds']//60} min")
    print(f"Cities: {', '.join(e['name'] for e in urls)}\n")

    async with async_playwright() as p:
        # --disable-blink-features=AutomationControlled makes navigator.webdriver
        # report the NATIVE `false` (default automated Chromium reports `true`,
        # which Cloudflare blocks — and masking it to `undefined` in JS is itself a
        # tell). With the flag, no JS patch is needed. This is the browser that
        # bootstraps the session cookies and serves as the Playwright fallback, so
        # it has to clear Cloudflare cleanly. (Same approach as browser.py.)
        browser = await p.chromium.launch(
            headless=config["headless"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

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
            "ip_banned_at": None,
            "http_session": http_session,
            "pw_context": context,
            "last_found": None,
            "booking_in_progress": False,
        }

        send_telegram(
            f"Argos started (fast HTTP mode).\n"
            f"Watching {', '.join(e['name'] for e in urls)} every "
            f"{state['interval_min']//60}-{state['interval_max']//60} min.\n"
            f"Commands: /screenshot, /status, /fast, /normal, /interval, /book"
        )

        asyncio.create_task(telegram_command_listener(state, page_lock, page, config))

        last_heartbeat = time.time()

        while True:
            now = datetime.now().strftime("%H:%M:%S")

            # Pause loop while IP banned — wait for /resume or auto-resume after timeout
            if state["ip_banned"]:
                banned_for = time.time() - (state["ip_banned_at"] or time.time())
                remaining = IP_BAN_AUTO_RESUME_AFTER - banned_for
                if remaining <= 0:
                    state["ip_banned"] = False
                    state["ip_banned_at"] = None
                    send_telegram(
                        f"Auto-resuming after {IP_BAN_AUTO_RESUME_AFTER//60} min ban pause. "
                        f"Fingers crossed the ban lifted..."
                    )
                    print(f"[{now}] Auto-resuming after IP ban timeout.")
                else:
                    print(f"[{now}] IP banned — paused. Auto-resume in {remaining/60:.1f} min. Send /resume to force.")
                    await asyncio.sleep(30)
                continue

            # Proactive session refresh every SESSION_REFRESH_INTERVAL
            if time.time() - last_session_refresh >= SESSION_REFRESH_INTERVAL:
                print(f"[{now}] Session refresh (proactive)...")
                async with page_lock:
                    http_session = await bootstrap_session(page, urls)
                state["http_session"] = http_session
                last_session_refresh = time.time()
                state["last_session_refresh"] = now

            loop = asyncio.get_event_loop()
            for entry in urls:
                try:
                    # Fast HTTP check — run in executor to avoid blocking event loop
                    available, html, elapsed, status_code = await loop.run_in_executor(
                        None, fast_check, http_session, entry, no_appt_phrase
                    )
                    state["fast_check_count"] += 1
                    status = "AVAILABLE !!!" if available else "unavailable"
                    print(f"[{now}] HTTP #{state['fast_check_count']} {entry['name']}: {status} ({elapsed*1000:.0f}ms)")

                    save_daily_snapshot(entry["name"], html)

                    if available:
                        notify(entry["name"], entry["url"], method="HTTP")
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        base = f"found_{entry['name']}_{ts}"
                        tarih_results = await report_dates_from_html(
                            http_session, entry, html, loop
                        )
                        dump_forensic_bundle(
                            base,
                            http_session=http_session,
                            html=html,
                            tarih_results=tarih_results,
                            status_code=status_code,
                            elapsed=elapsed,
                            detector="HTTP",
                        )
                        async with page_lock:
                            if config.get("screenshot_on_found"):
                                try:
                                    await page.goto(entry["url"], timeout=30_000)
                                    try:
                                        await page.wait_for_selector(
                                            ".preloader, #preloader, .loading-overlay",
                                            state="hidden", timeout=10_000,
                                        )
                                    except Exception:
                                        pass
                                    await asyncio.sleep(3)
                                except Exception as nav_e:
                                    print(f"  [Screenshot] Navigation failed: {nav_e}")
                                await take_and_send_screenshot(
                                    page, base, caption=f"{entry['name']} - {ts}"
                                )
                        # Stash for /book manual trigger
                        state["last_found"] = {
                            "entry": entry, "tarih_results": tarih_results,
                            "base": base,
                        }
                        if config.get("auto_book") and not state.get("booking_in_progress"):
                            profile = load_booking_profile()
                            if not profile:
                                send_telegram(
                                    f"Auto-book is on but {BOOKING_PROFILE_PATH} is missing — skipping booker."
                                )
                            else:
                                state["booking_in_progress"] = True
                                async def _book_then_clear(entry, http_session, pw_context, profile, tarih_results, base, config):
                                    try:
                                        await fast_track_book(entry, http_session, pw_context, profile, tarih_results, base, config)
                                    finally:
                                        state["booking_in_progress"] = False
                                asyncio.create_task(_book_then_clear(entry, http_session, context, profile, tarih_results, base, config))

                except IPBannedError as e:
                    print(f"[{now}] {e}")
                    if not state["ip_banned"]:
                        state["ip_banned"] = True
                        state["ip_banned_at"] = time.time()
                        send_telegram(
                            f"IP banned by Cloudflare — checks paused.\n"
                            f"Restart your modem, then send /resume.\n"
                            f"Will auto-resume in {IP_BAN_AUTO_RESUME_AFTER//60} min if not resumed manually."
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

                            # Prefer structural check on full HTML; fall back to phrase on text
                            has_form = FORM_MARKER in html and CSRF_MARKER in html
                            if has_form:
                                available = True
                            else:
                                available = no_appt_phrase not in text
                            state["playwright_check_count"] += 1
                            status = "AVAILABLE !!!" if available else "unavailable"
                            print(f"[{now}] PW  #{state['playwright_check_count']} {entry['name']}: {status}")

                            save_daily_snapshot(entry["name"], html)

                            if available:
                                notify(entry["name"], entry["url"], method="Playwright")
                                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                                base = f"found_{entry['name']}_{ts}"
                                tarih_results = await report_dates_from_html(
                                    http_session, entry, html, loop
                                )
                                # html here is page.content() (rendered DOM); pass as both
                                # raw html and dom_html so captcha/markers are extracted
                                # and the DOM is preserved alongside if it differs.
                                dump_forensic_bundle(
                                    base,
                                    http_session=http_session,
                                    html=html,
                                    dom_html=html,
                                    tarih_results=tarih_results,
                                    status_code=200,
                                    elapsed=None,
                                    detector="Playwright",
                                )
                                if config.get("screenshot_on_found"):
                                    await take_and_send_screenshot(
                                        page, base, caption=f"{entry['name']} - {ts}"
                                    )
                                state["last_found"] = {
                                    "entry": entry, "tarih_results": tarih_results,
                                    "base": base,
                                }
                                if config.get("auto_book") and not state.get("booking_in_progress"):
                                    profile = load_booking_profile()
                                    if not profile:
                                        send_telegram(
                                            f"Auto-book is on but {BOOKING_PROFILE_PATH} is missing — skipping booker."
                                        )
                                    else:
                                        state["booking_in_progress"] = True
                                        async def _book_then_clear(entry, http_session, pw_context, profile, tarih_results, base, config):
                                            try:
                                                await fast_track_book(entry, http_session, pw_context, profile, tarih_results, base, config)
                                            finally:
                                                state["booking_in_progress"] = False
                                        asyncio.create_task(_book_then_clear(entry, http_session, context, profile, tarih_results, base, config))

                            # Refresh cookies from this successful Playwright load
                            cookies = await page.context.cookies()
                            for c in cookies:
                                http_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
                            last_session_refresh = time.time()
                            state["last_session_refresh"] = now
                            print("  [Session] Cookies refreshed after fallback")

                    except Exception as e2:
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
