"""
Manual capture mode: launches Chrome, opens the booking page, and records
EVERYTHING (network + headers, console, dialogs, navigations, and a DOM+screenshot
snapshot on every structural change) while you do the booking by hand.

Run with: python -m visa.capture [city]
  city: Istanbul (default) or Ankara

Design constraints (important):
  * We NEVER inject observers/expose_function/window overrides into the page.
    Those trip Cloudflare bot detection and have cost real slots before. The only
    init script is the standard automation-marker scrub (it removes signals, it
    does not add any). Everything else is captured out-of-band via CDP (screenshots,
    DOM serialization) or via passive Playwright event listeners — none of which the
    page can see.
  * Popups here (SweetAlert "Emin misiniz?", result dialogs) are pure client-side
    JS and make NO network request, so they can only be caught by watching the DOM.
    We poll ~1.5s and snapshot on every structural change, so an opening/closing
    dialog is captured both as HTML and as a screenshot.
  * The bundle is auto-saved every few seconds, so a hard kill can't wipe it.
"""
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from visa.browser import cdp_url, launch_chrome_cdp
from visa.config import load_config
from visa.telegram import send_telegram, send_telegram_photo


BASE_DIR = Path(".")
POLL_SECONDS = 1.5          # DOM/dialog poll cadence
HEARTBEAT_EVERY = 8         # force a timelapse screenshot every N idle ticks
AUTOSAVE_EVERY = 4          # write the bundle every N ticks (~6s)


def _bundle_path(city: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(BASE_DIR / f"capture_{city}_{ts}")


async def run_capture(city: str = "Istanbul"):
    config = load_config()

    entry = next(
        (e for e in config.get("urls", []) if e["name"].lower() == city.lower()),
        None,
    )
    if not entry:
        print(f"[Capture] Unknown city '{city}'. Available: {[e['name'] for e in config.get('urls', [])]}")
        return

    booking_url = entry["url"]
    base = _bundle_path(city)
    print(f"\n[Capture] City: {city}")
    print(f"[Capture] URL: {booking_url}")
    print(f"[Capture] Bundle prefix: {base}")
    print(f"[Capture] Launching Chrome...")

    cdp_ok = await asyncio.get_event_loop().run_in_executor(None, launch_chrome_cdp, config)
    if not cdp_ok:
        print("[Capture] Could not launch Chrome.")
        return

    # ── Everything we record ────────────────────────────────────────────────
    requests_log = []
    responses_log = []
    console_log = []
    dialogs_log = []       # native JS dialogs (alert/confirm/beforeunload)
    popups_log = []        # new tabs/windows
    navigations_log = []
    dom_snapshots = []     # {ts, url, file, changed_because, has_dialog}
    screenshots = []
    cookies_snapshot = []
    shot_count = [0]
    snap_count = [0]

    def _save_bundle(note=""):
        bundle = {
            "city": city,
            "booking_url": booking_url,
            "saved_at": datetime.now().isoformat(),
            "note": note,
            "requests": requests_log,
            "responses": responses_log,
            "console": console_log,
            "dialogs": dialogs_log,
            "popups": popups_log,
            "navigations": navigations_log,
            "dom_snapshots": dom_snapshots,
            "screenshots": screenshots,
            "cookies": cookies_snapshot,
        }
        try:
            Path(f"{base}.capture.json").write_text(
                json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            print(f"[Capture] Bundle save failed: {e}")

    async def _screenshot(page, label="", full_page=True):
        try:
            shot_count[0] += 1
            path = f"{base}.shot{shot_count[0]:03d}_{label}.png" if label else f"{base}.shot{shot_count[0]:03d}.png"
            await page.screenshot(path=path, full_page=full_page)
            screenshots.append({
                "n": shot_count[0], "ts": datetime.now().isoformat(),
                "label": label, "path": path, "url": page.url,
            })
            return path
        except Exception as e:
            print(f"[Capture] Screenshot failed ({label}): {e}")
            return None

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url(config))
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()

        # Automation-marker scrub ONLY (removes signals, adds none) — same as booker.py.
        await page.add_init_script("""
            try { delete window.__playwright; } catch(e) {}
            try { delete window.__pwInitScripts; } catch(e) {}
            try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array; } catch(e) {}
            try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise; } catch(e) {}
        """)

        # ── Network capture (with full headers) ─────────────────────────────
        def _on_request(req):
            try:
                headers = dict(req.headers)
            except Exception:
                headers = {}
            requests_log.append({
                "ts": datetime.now().isoformat(),
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "headers": headers,
                "post_data": req.post_data or "",
            })
            if req.method == "POST":
                print(f"[Capture] POST -> {req.url[:90]}")

        async def _on_response(resp):
            if "as-visa.com" not in resp.url:
                return
            try:
                body = (await resp.body()).decode("utf-8", errors="replace")
            except Exception:
                body = ""
            try:
                headers = dict(resp.headers)
            except Exception:
                headers = {}
            # Keep FULL bodies for the booking-page HTML and for JS (inline or
            # external /PageJs/*.js) — the submit button + dialog wiring lives there.
            # Cap everything else so the bundle stays reasonable.
            u = resp.url.lower()
            keep_full = (
                u.rstrip("/").endswith("bireysel-basvuru")
                or u.endswith(".js")
                or "/pagejs/" in u
            )
            responses_log.append({
                "ts": datetime.now().isoformat(),
                "status": resp.status,
                "url": resp.url,
                "headers": headers,
                "body": body if keep_full else body[:4000],
            })
            if resp.request.method == "POST":
                print(f"[Capture] Response {resp.status} <- {resp.url[:80]}")
                print(f"[Capture]   body: {body[:200]}")

        # ── Passive listeners (page can't see any of these) ─────────────────
        def _on_console(msg):
            try:
                console_log.append({
                    "ts": datetime.now().isoformat(),
                    "type": msg.type, "text": msg.text,
                })
            except Exception:
                pass

        def _on_pageerror(err):
            console_log.append({
                "ts": datetime.now().isoformat(),
                "type": "pageerror", "text": str(err),
            })

        async def _on_dialog(dialog):
            # Native JS dialogs only (SweetAlert is NOT native — handled by DOM poll).
            dialogs_log.append({
                "ts": datetime.now().isoformat(),
                "type": dialog.type, "message": dialog.message,
            })
            print(f"[Capture] NATIVE DIALOG ({dialog.type}): {dialog.message[:120]}")
            try:
                await dialog.accept()   # don't block the user's flow
            except Exception:
                pass

        def _on_popup(new_page):
            popups_log.append({
                "ts": datetime.now().isoformat(),
                "url": getattr(new_page, "url", ""),
            })
            print(f"[Capture] POPUP/new tab: {getattr(new_page, 'url', '')[:90]}")

        def _on_framenav(frame):
            try:
                if frame == page.main_frame:
                    navigations_log.append({
                        "ts": datetime.now().isoformat(), "url": frame.url,
                    })
                    print(f"[Capture] NAV -> {frame.url[:90]}")
            except Exception:
                pass

        def _on_reqfailed(req):
            requests_log.append({
                "ts": datetime.now().isoformat(),
                "method": req.method, "url": req.url,
                "failed": (req.failure or ""),
            })

        page.on("request", _on_request)
        page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))
        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        page.on("dialog", lambda d: asyncio.ensure_future(_on_dialog(d)))
        page.on("popup", _on_popup)
        page.on("framenavigated", _on_framenav)
        page.on("requestfailed", _on_reqfailed)

        # ── Navigate to booking page ────────────────────────────────────────
        print(f"[Capture] Navigating to {booking_url} ...")
        await page.goto(booking_url, timeout=30_000)

        send_telegram(
            f"Manual capture started — {city}\n"
            "Fill + submit the form yourself. Every popup, request, console line and "
            "DOM change is being recorded, and the bundle auto-saves every few seconds."
        )
        print("\n[Capture] *** Browser is open. Do the booking manually. ***")
        print("[Capture] Everything auto-saves; Ctrl+C or close the tab when done.\n")

        # ── Main watch loop: snapshot on every structural change ────────────
        async def _watch():
            last_hash = None
            idle_ticks = 0
            tick = 0
            while True:
                await asyncio.sleep(POLL_SECONDS)
                tick += 1
                if "as-visa.com" not in (page.url or ""):
                    continue
                try:
                    dom = await page.content()
                except Exception:
                    continue

                h = hashlib.md5(dom.encode("utf-8", "replace")).hexdigest()
                # A SweetAlert / modal is detectable purely from the DOM string we
                # already have — no extra in-page call needed. But we must match an
                # actually-rendered element, NOT the SweetAlert CSS/JS that is always
                # embedded in the page: `swal2-popup` etc. appear in the stylesheet and
                # library source at all times. SweetAlert injects a real
                # `<div class="swal2-container ...">` and sets `swal2-shown` on <body>
                # ONLY while a dialog is open, so anchor the match to those elements.
                has_dialog = bool(
                    re.search(r'<div[^>]*class="[^"]*swal2-container', dom)
                    or re.search(r'<body[^>]*class="[^"]*swal2-shown', dom)
                    or re.search(r'<div[^>]*class="[^"]*modal[^"]*\bshow\b', dom)
                )

                if h != last_hash:
                    last_hash = h
                    idle_ticks = 0
                    snap_count[0] += 1
                    ts = datetime.now().strftime("%H%M%S_%f")[:-3]
                    dom_file = f"{base}.dom_{snap_count[0]:03d}_{ts}.html"
                    try:
                        Path(dom_file).write_text(dom, encoding="utf-8")
                    except Exception:
                        dom_file = ""
                    dom_snapshots.append({
                        "n": snap_count[0], "ts": datetime.now().isoformat(),
                        "url": page.url, "file": dom_file, "has_dialog": has_dialog,
                    })
                    # Full-page screenshot for every distinct state (captures popups).
                    label = "dialog" if has_dialog else "state"
                    await _screenshot(page, f"{label}{snap_count[0]:03d}", full_page=True)
                    if has_dialog:
                        print(f"[Capture] *** DIALOG/modal in DOM — snapshot #{snap_count[0]} ***")
                        shot = screenshots[-1]["path"] if screenshots else None
                        if shot:
                            try:
                                send_telegram_photo(shot, caption=f"Popup captured ({city})")
                            except Exception:
                                pass
                else:
                    idle_ticks += 1
                    if idle_ticks % HEARTBEAT_EVERY == 0:
                        await _screenshot(page, "heartbeat", full_page=False)

                # Refresh cookies + autosave periodically so a hard kill loses little.
                if tick % AUTOSAVE_EVERY == 0:
                    try:
                        cookies_snapshot.clear()
                        cookies_snapshot.extend(await ctx.cookies())
                    except Exception:
                        pass
                    _save_bundle(note="autosave")

        watch_task = asyncio.ensure_future(_watch())

        try:
            await page.wait_for_event("close", timeout=0)
            print("[Capture] Chrome tab closed.")
        except KeyboardInterrupt:
            pass
        except Exception:
            pass
        finally:
            watch_task.cancel()
            # Final DOM + full-page screenshot + cookies + save.
            try:
                dom = await page.content()
                Path(f"{base}.final.dom.html").write_text(dom, encoding="utf-8")
            except Exception:
                pass
            await _screenshot(page, "final", full_page=True)
            try:
                cookies_snapshot.clear()
                cookies_snapshot.extend(await ctx.cookies())
            except Exception:
                pass
            _save_bundle(note="final")
            print(f"[Capture] Bundle saved: {base}.capture.json "
                  f"({len(requests_log)} req, {len(responses_log)} resp, "
                  f"{len(dom_snapshots)} DOM snaps, {len(screenshots)} shots)")
            send_telegram(
                f"Manual capture complete ({city}). "
                f"{len(dom_snapshots)} DOM snapshots, {len(screenshots)} screenshots, "
                f"{len([d for d in dom_snapshots if d['has_dialog']])} with a popup on screen."
            )


def main():
    city = sys.argv[1] if len(sys.argv) > 1 else "Istanbul"
    asyncio.run(run_capture(city))


if __name__ == "__main__":
    main()
