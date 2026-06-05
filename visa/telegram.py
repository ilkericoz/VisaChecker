import asyncio
import os
import time

import requests as req_lib

from visa.config import (
    IP_BAN_AUTO_RESUME_AFTER, BOOKING_PROFILE_PATH, TARIH_GETIR_BASE,
    load_booking_profile,
)


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


async def telegram_command_listener(state, page_lock, page, config):
    # Local imports to avoid circular deps (detector/booker import telegram at module level)
    from visa.detector import extract_form_fields
    from visa.booker import fast_track_book

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
                            from datetime import datetime
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
                if state["ip_banned"] and state["ip_banned_at"]:
                    remaining = IP_BAN_AUTO_RESUME_AFTER - (time.time() - state["ip_banned_at"])
                    banned = f" ⚠️ IP BANNED — auto-resume in {max(0, remaining/60):.1f} min (or /resume now)"
                elif state["ip_banned"]:
                    banned = " ⚠️ IP BANNED — send /resume after modem restart"
                else:
                    banned = ""
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

            elif cmd == "/compare":
                print("[CMD] /compare")
                send_telegram("Comparing HTTP vs Playwright for each city...")
                no_appt = config["no_appointment_phrase"]
                http_sess = state.get("http_session")
                if not http_sess:
                    send_telegram("No HTTP session available yet.")
                else:
                    from visa.config import SANITY_PHRASE
                    lines = []
                    for entry in urls:
                        try:
                            t0 = time.time()
                            r = http_sess.get(entry["url"], timeout=15)
                            http_elapsed = time.time() - t0
                            http_html = r.text
                            http_code = r.status_code
                            http_has_sanity = SANITY_PHRASE in http_html
                            http_has_no_appt = no_appt in http_html
                            http_size = len(http_html)
                            if not http_has_sanity:
                                http_verdict = "INVALID (sanity missing)"
                            elif not http_has_no_appt:
                                http_verdict = "SLOTS OPEN"
                            else:
                                http_verdict = "no slots"
                        except Exception as e:
                            http_verdict = f"ERROR: {e}"
                            http_elapsed = 0
                            http_size = 0
                            http_code = 0

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
                                pw_html = await page.content()
                            pw_has_sanity = SANITY_PHRASE in pw_html
                            pw_has_no_appt = no_appt in pw_html
                            pw_size = len(pw_html)
                            if not pw_has_sanity:
                                pw_verdict = "INVALID (sanity missing)"
                            elif not pw_has_no_appt:
                                pw_verdict = "SLOTS OPEN"
                            else:
                                pw_verdict = "no slots"
                        except Exception as e:
                            pw_verdict = f"ERROR: {e}"
                            pw_size = 0

                        match = "MATCH" if http_verdict == pw_verdict else "*** MISMATCH ***"
                        lines.append(
                            f"{entry['name']}:\n"
                            f"  HTTP  ({http_elapsed*1000:.0f}ms, {http_size} bytes, {http_code}): {http_verdict}\n"
                            f"  PW    ({pw_size} bytes): {pw_verdict}\n"
                            f"  → {match}"
                        )

                    send_telegram("Compare results:\n\n" + "\n\n".join(lines))

            elif cmd == "/extract":
                print("[CMD] /extract")
                send_telegram("Loading each city page to scrape form fields (only useful when slots are open)...")
                for entry in urls:
                    async with page_lock:
                        await extract_form_fields(page, entry)

            elif cmd == "/book":
                print("[CMD] /book")
                last = state.get("last_found")
                if not last:
                    send_telegram(
                        "No detected slot to book. /book triggers the fast-track "
                        "booker against the most recent detection — none yet."
                    )
                elif state.get("booking_in_progress"):
                    send_telegram("A booking is already in progress.")
                else:
                    profile = load_booking_profile()
                    if not profile:
                        send_telegram(
                            f"{BOOKING_PROFILE_PATH} missing or unreadable. "
                            f"Copy booking_profile.example.json and fill it."
                        )
                    else:
                        state["booking_in_progress"] = True
                        async def _manual_book():
                            try:
                                await fast_track_book(
                                    last["entry"], state["http_session"],
                                    state["pw_context"],
                                    profile, last["tarih_results"], last["base"], config,
                                )
                            finally:
                                state["booking_in_progress"] = False
                        asyncio.create_task(_manual_book())

            elif cmd == "/probe":
                print("[CMD] /probe")
                send_telegram("Probing TarihGetir with tabId 1-15 for each city...")
                http_sess = state.get("http_session")
                if not http_sess:
                    send_telegram("No HTTP session yet.")
                else:
                    for entry in urls:
                        api_path = entry.get("tarih_getir_path")
                        if not api_path:
                            send_telegram(f"{entry['name']}: no tarih_getir_path in config")
                            continue

                        csrf = None
                        try:
                            async with page_lock:
                                await page.goto(entry["url"], timeout=30_000)
                                await asyncio.sleep(3)
                                try:
                                    csrf = await page.eval_on_selector(
                                        'input[name="__RequestVerificationToken"]',
                                        "el => el.value",
                                    )
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        api_url = TARIH_GETIR_BASE + api_path
                        lines = [f"Probe {entry['name']} → {api_path}"]
                        lines.append(f"CSRF: {'found' if csrf else 'not found — trying without'}")

                        country_candidates = ["TÜRKİYE", "TURKEY", "TR", "1"]
                        for countryid in country_candidates:
                            for tab_id in range(1, 16):
                                try:
                                    headers = {}
                                    if csrf:
                                        headers["RequestVerificationToken"] = csrf
                                    r = await asyncio.get_event_loop().run_in_executor(
                                        None,
                                        lambda: http_sess.post(
                                            api_url,
                                            data={"tabId": tab_id, "countryid": countryid},
                                            headers=headers,
                                            timeout=10,
                                        ),
                                    )
                                    body = r.text.strip()[:120]
                                    if r.status_code == 200 and body not in ("", "null", "[]", "false"):
                                        lines.append(f"  *** HIT: countryid={countryid!r} tabId={tab_id} → {r.status_code} {body}")
                                    else:
                                        lines.append(f"  countryid={countryid!r} tabId={tab_id} → {r.status_code} {body or '(empty)'}")
                                except Exception as e:
                                    lines.append(f"  countryid={countryid!r} tabId={tab_id} → ERROR: {e}")

                        send_telegram("\n".join(lines))

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
