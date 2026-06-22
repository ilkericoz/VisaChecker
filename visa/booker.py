import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import winsound
from playwright.async_api import async_playwright

from visa.config import USER_AGENT
from visa.telegram import send_telegram, send_telegram_photo
from visa.browser import launch_chrome_cdp, cdp_url
from visa.captcha import solve_entered_code, solve_recaptcha, solve_turnstile
from visa.date_api import pick_date
from visa.form_filler import fill_form


async def fast_track_book(entry, http_session, pw_context, profile, tarih_results, base, config):
    """
    Open a HEADED browser window (real Chrome via CDP), fill all personal-info
    fields from the profile, wait out the anti-bot timer, and auto-submit.
    Honeypot fields (hp_*) are never touched — only mapped fields are filled.
    """
    label, picked_date = pick_date(tarih_results, profile.get("date_preference"))
    if picked_date:
        send_telegram(
            f"Fast-track: opening {entry['name']} booking window.\n"
            f"Suggested date: {picked_date} ({label}).\n"
            f"Pre-filling personal info — type captcha + click Randevu Al when ready."
        )
    else:
        send_telegram(
            f"Fast-track: no date matched profile preference; opening window "
            f"with personal info pre-filled. Pick a date manually."
        )

    try:
        winsound.Beep(880, 600)
    except Exception:
        pass

    try:
        async with async_playwright() as p:
            # Prefer real Chrome via CDP — avoids Cloudflare bot detection entirely.
            # launch_chrome_cdp() starts Chrome with the dedicated visa profile on port 9222
            # (or connects if it's already running). Falls back to a plain Playwright
            # browser with a Telegram warning if Chrome can't be found/started.
            cdp_ok = await asyncio.get_event_loop().run_in_executor(
                None, launch_chrome_cdp, config
            )
            using_cdp = False
            if cdp_ok:
                try:
                    browser = await p.chromium.connect_over_cdp(cdp_url(config))
                    # Use the existing profile context so Cloudflare cookies are intact.
                    book_context = browser.contexts[0] if browser.contexts else await browser.new_context()

                    # Guard against attaching to the WRONG Chrome — e.g. another bot
                    # (FareHarbor) already holds this port with a different profile.
                    # If we see a foreign tab, the session/cookies aren't ours and a
                    # submit would look like "unusual activity" to the server.
                    try:
                        foreign = []
                        for ctx in browser.contexts:
                            for pg in ctx.pages:
                                u = (pg.url or "").lower()
                                if u and "as-visa.com" not in u and "about:blank" not in u and "newtab" not in u and "chrome://" not in u:
                                    foreign.append(pg.url)
                        if foreign:
                            send_telegram(
                                "Fast-track: ABORTED — connected Chrome has foreign tabs "
                                f"({foreign[:2]}). Another bot is likely sharing CDP port "
                                f"{cdp_url(config)}. Give each bot its own cdp_port + profile. "
                                "Not submitting with a contaminated session."
                            )
                            print(f"[Booker] Foreign tabs on shared CDP: {foreign}")
                            return
                    except Exception as _ge:
                        print(f"[Booker] foreign-tab guard error: {_ge}")

                    using_cdp = True
                    print("[Booker] Connected to real Chrome via CDP")
                except Exception as e:
                    print(f"[Booker] CDP connect failed: {e} — falling back to Playwright browser")
                    cdp_ok = False

            if not cdp_ok:
                send_telegram(
                    "Fast-track: Chrome CDP unavailable — using fallback browser. "
                    "Cloudflare may block. To fix: check README for chrome_visa_profile setup."
                )
                headless = not config.get("headed_on_book", True)
                browser = await p.chromium.launch(headless=headless)
                book_context = await browser.new_context(user_agent=USER_AGENT)
                try:
                    cookie_list = await pw_context.cookies()
                    if cookie_list:
                        await book_context.add_cookies(cookie_list)
                except Exception as e:
                    print(f"[Booker] cookie inject failed: {e}")

            page = await book_context.new_page()

            # Scrub Playwright automation markers before any page loads.
            # CDP connection sets window.__playwright and similar properties that
            # Cloudflare Turnstile uses to detect automated browsers.
            # NOTE: we do NOT touch navigator.webdriver here — the
            # --disable-blink-features=AutomationControlled launch flag already
            # makes it report the native `false`. Forcing it to `undefined` in JS
            # is a stronger tell than leaving it false, so we leave it alone.
            await page.add_init_script("""
                try { delete window.__playwright; } catch(e) {}
                try { delete window.__pwInitScripts; } catch(e) {}
                try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array; } catch(e) {}
                try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise; } catch(e) {}
            """)


            # Capture every POST to the appointment domain — both outgoing payload
            # and server response body, so a silent server-side rejection is visible.
            submit_captures = []
            submit_responses = []

            def _on_request(request):
                if request.method == "POST" and "appointment.as-visa.com" in request.url:
                    try:
                        submit_captures.append({
                            "url": request.url,
                            "post_data": request.post_data,
                            "headers": {
                                k: v for k, v in request.headers.items()
                                if k.lower() in (
                                    "content-type", "referer", "x-requested-with",
                                    "__requestverificationtoken",
                                )
                            },
                        })
                    except Exception:
                        pass

            async def _on_response(response):
                if response.request.method == "POST" and "appointment.as-visa.com" in response.url:
                    try:
                        body = await response.body()
                        submit_responses.append({
                            "url": response.url,
                            "status": response.status,
                            "body": body.decode("utf-8", errors="replace")[:4000],
                        })
                    except Exception:
                        pass

            page.on("request", _on_request)
            page.on("response", _on_response)

            await page.goto(entry["url"], timeout=30_000)
            try:
                await page.wait_for_selector("#apForm", timeout=15_000)
            except Exception:
                send_telegram(
                    "Fast-track: form didn't render within 15s — slot may have closed. "
                    "Window left open so you can investigate."
                )
                return

            # Capture JS-rendered DOM for diagnostics (form fields injected by wizard JS
            # won't appear in the HTTP response snapshot — this captures what's actually there).
            try:
                dom_html = await page.content()
                Path(f"{base}.dom.html").write_text(dom_html, encoding="utf-8")
            except Exception as _de:
                print(f"[Booker] DOM capture failed: {_de}")

            page_load_time = time.time()
            filled, failed, times = await fill_form(page, profile, entry, http_session, picked_date)

            # Screenshot of the pre-filled form — sent to Telegram immediately
            # so you can verify field state even if you're away from the screen.
            try:
                shot_path = f"{base}.prefilled.png"
                await page.screenshot(path=shot_path, full_page=True)
                send_telegram_photo(shot_path, caption=f"Pre-filled form — {entry['name']} ({picked_date or 'no date'})")
                print(f"  Pre-filled screenshot: {shot_path}")
            except Exception as e:
                print(f"[Booker] screenshot failed: {e}")

            time_line = ""
            if times:
                time_line = f"\nTime slots loaded ({len(times)}): {', '.join(s.get('text','') for s in times[:4])}"
                if len(times) > 4:
                    time_line += f" (+{len(times)-4} more)"
            elif picked_date:
                time_line = "\nNo time slots returned from SaatGetir — pick manually."

            send_telegram(
                f"Fast-track: filled {len(filled)} fields"
                + (f", {len(failed)} failed" if failed else "")
                + ".\nWaiting for anti-bot timer, then auto-submitting..."
                + time_line
                + (f"\nFailed: {'; '.join(failed[:5])}" if failed else "")
            )

            # Auto-submit: wait out the anti-bot dwell timer, confirm Turnstile
            # completed, click submit, and handle the two SweetAlert dialogs.
            #
            # The server rejects with "işleminiz çok hızlı yapıldığı için ... bot
            # olarak algıladı" if (now - formStartTime) is under its threshold.
            # formStartTime is a hidden field the page JS sets on load (epoch ms).
            # We gate on THAT field — the server's own reference clock — rather
            # than our local page_load_time, since the two can drift apart.
            MIN_ELAPSED_SECS = 60  # margin over observed ~40s server threshold

            async def _server_elapsed():
                try:
                    fst = await page.evaluate(
                        "() => document.getElementById('formStartTime')?.value || ''"
                    )
                    if fst and fst.isdigit():
                        return (time.time() * 1000 - int(fst)) / 1000.0
                except Exception:
                    pass
                # Fall back to our local clock if the field is missing/unreadable.
                return time.time() - page_load_time

            elapsed_so_far = await _server_elapsed()
            wait_more = max(2.0, MIN_ELAPSED_SECS - elapsed_so_far)
            if wait_more > 2:
                send_telegram(
                    f"Fast-track: dwell so far {elapsed_so_far:.0f}s — waiting "
                    f"{wait_more:.0f}s more before submit (anti-bot timer)..."
                )
                await asyncio.sleep(wait_more)
            # Re-check after sleeping in case formStartTime resolved differently.
            final_elapsed = await _server_elapsed()
            if final_elapsed < MIN_ELAPSED_SECS:
                extra = MIN_ELAPSED_SECS - final_elapsed
                await asyncio.sleep(extra)

            # Fill enteredCode CAPTCHA here — NOT during form fill — because the
            # 6-digit code expires in ~60s and the anti-bot timer eats most of that.
            try:
                entered_code, ec_err = await solve_entered_code(page)
                if entered_code:
                    await page.fill('input[name="enteredCode"]', entered_code)
                    filled.append(f"enteredCode ({entered_code})")
                    print(f"[Booker] enteredCode filled: {entered_code}")
                else:
                    failed.append(f"enteredCode: {ec_err}")
                    send_telegram(f"Fast-track: enteredCode solve failed: {ec_err}")
            except Exception as _ee:
                failed.append(f"enteredCode: {_ee}")

            cf_val = await solve_turnstile(page)
            rc_val = await solve_recaptcha(page)

            # Snapshot every visible form field value right before submit.
            # This is the ground truth for what will actually be sent — catches any
            # field that silently failed to register (widget state, readonly inputs, etc).
            form_state = {}
            try:
                form_state = await page.evaluate(
                    "() => {"
                    "  const out = {};"
                    "  document.querySelectorAll('#apForm input, #apForm select, #apForm textarea').forEach(el => {"
                    "    const key = el.name || el.id;"
                    "    if (key && !key.startsWith('hp_')) out[key] = el.value;"
                    "  });"
                    "  return out;"
                    "}"
                )
                Path(f"{base}.booker.json").write_text(
                    json.dumps({
                        "filled": filled, "failed": failed,
                        "picked_date": picked_date, "label": label,
                        "time_slots": times,
                        "form_state_pre_submit": form_state,
                    }, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"[Booker] pre-submit form dump failed: {e}")

            try:
                rc_required = bool(await page.evaluate("() => !!window.recaptchaSiteKey"))
            except Exception:
                rc_required = False

            captcha_ok = cf_val and (not rc_required or rc_val)

            # Shared post-submit handler: dismisses confirmation dialogs, captures result.
            async def _handle_post_submit():
                # First SweetAlert: "Are you sure?" → Evet
                await page.wait_for_selector('.swal2-confirm', timeout=15_000)
                await page.click('.swal2-confirm')
                print("[Booker] Clicked SweetAlert Evet")
                # Second SweetAlert: result/redirect → Tamam
                try:
                    await page.wait_for_selector('.swal2-confirm', timeout=30_000)
                    # Screenshot the dialog before dismissing — contains server message
                    ts_d = datetime.now().strftime("%Y%m%d_%H%M%S")
                    shot_d = f"{base}.dialog_{ts_d}.png"
                    try:
                        await page.screenshot(path=shot_d, full_page=True)
                        send_telegram_photo(shot_d, caption="Server response dialog")
                    except Exception:
                        pass
                    await page.click('.swal2-confirm')
                    print("[Booker] Clicked SweetAlert Tamam")
                except Exception:
                    pass
                await page.wait_for_load_state('load', timeout=30_000)
                final_url = page.url
                ts2 = datetime.now().strftime("%Y%m%d_%H%M%S")
                shot = f"{base}.success_{ts2}.png"
                await page.screenshot(path=shot, full_page=True)
                send_telegram(f"BOOKING SUBMITTED! Final URL: {final_url}")
                send_telegram_photo(shot, caption=f"Booking result — {entry['name']}")

            # Dismiss informational SweetAlert (appears on page load, unrelated to submit)
            async def _dismiss_info_popup():
                try:
                    if await page.query_selector('.swal2-popup:visible'):
                        await page.click('.swal2-confirm')
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

            if not captcha_ok:
                missing = []
                if not cf_val:
                    missing.append("cfToken")
                if rc_required and not rc_val:
                    missing.append("recaptchaToken")
                send_telegram(
                    f"Fast-track: CAPTCHA unsolved ({', '.join(missing)}) — "
                    "leaving window open for manual submit."
                )
            elif profile.get("manual_submit", False):
                # Manual submit mode: bot preps everything, user clicks the button.
                # Bot watches dialogs and captures the server response.
                await _dismiss_info_popup()
                msg = (
                    f"READY TO SUBMIT — {entry['name']} {picked_date} {times[0]['text'] if times else ''}\n"
                    f"cfToken: set | recaptchaToken: {'set' if rc_val else 'n/a'}\n"
                    "Click RANDEVU AL in Chrome now. I will handle the confirmation dialogs."
                )
                send_telegram(msg)
                print(f"\n[Booker] *** MANUAL SUBMIT MODE — click the button in Chrome ***\n")
                try:
                    await _handle_post_submit()
                except Exception as e:
                    try:
                        shot_fail = f"{base}.fail_{datetime.now().strftime('%H%M%S')}.png"
                        await page.screenshot(path=shot_fail, full_page=True)
                        send_telegram_photo(shot_fail, caption=f"Post-submit capture: {e}")
                    except Exception:
                        pass
                    send_telegram(f"Fast-track: post-submit capture failed: {e}")
            else:
                try:
                    await _dismiss_info_popup()
                    send_telegram(
                        f"Fast-track: submitting... "
                        f"(cfToken=set, recaptchaToken={'set' if rc_val else 'n/a'})"
                    )
                    await page.click('#randevuAlButton')
                    await _handle_post_submit()
                except Exception as e:
                    try:
                        shot_fail = f"{base}.fail_{datetime.now().strftime('%H%M%S')}.png"
                        await page.screenshot(path=shot_fail, full_page=True)
                        send_telegram_photo(shot_fail, caption=f"Auto-submit failed: {e}")
                    except Exception:
                        pass
                    send_telegram(f"Fast-track: auto-submit failed: {e} — leaving window open.")

            # Keep window open so user can see the result or intervene
            try:
                await page.wait_for_event("close", timeout=0)
            except Exception:
                pass

            # Dump all POSTs (outgoing + server responses) captured while the window was open.
            if submit_captures or submit_responses:
                try:
                    Path(f"{base}.submit.json").write_text(
                        json.dumps({"requests": submit_captures, "responses": submit_responses},
                                   indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    print(f"  Submit captured: {base}.submit.json "
                          f"({len(submit_captures)} req, {len(submit_responses)} resp)")
                    if submit_captures:
                        last_req = submit_captures[-1]
                        send_telegram(
                            f"Submit captured ({len(submit_captures)} POST):\n"
                            f"{last_req['url']}\n"
                            f"Payload: {str(last_req.get('post_data', ''))[:300]}"
                        )
                    if submit_responses:
                        last_resp = submit_responses[-1]
                        send_telegram(
                            f"Server response ({last_resp['status']}):\n"
                            f"{last_resp['body'][:500]}"
                        )
                except Exception as e:
                    print(f"[Booker] submit dump failed: {e}")

            # When using CDP we only close the tab — leave Chrome running so the
            # profile stays warm for the next detection event.
            if not using_cdp:
                try:
                    await browser.close()
                except Exception:
                    pass

    except Exception as e:
        send_telegram(f"Fast-track booker crashed: {e}")
        print(f"[Booker] crashed: {e}")
