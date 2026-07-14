"""
Manual capture mode: launches Chrome, opens the Istanbul booking page,
and records everything (network, screenshots, DOM) while you do the booking
manually. Run with: python -m visa.capture [city]
  city: Istanbul (default) or Ankara
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from visa.browser import cdp_url, launch_chrome_cdp
from visa.config import load_config
from visa.telegram import send_telegram, send_telegram_photo


BASE_DIR = Path(".")


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

    requests_log = []
    responses_log = []
    screenshots = []
    shot_count = [0]

    async def _screenshot(page, label=""):
        try:
            shot_count[0] += 1
            path = f"{base}.shot{shot_count[0]:03d}_{label}.png" if label else f"{base}.shot{shot_count[0]:03d}.png"
            await page.screenshot(path=path, full_page=True)
            screenshots.append({"n": shot_count[0], "label": label, "path": path, "url": page.url})
            print(f"[Capture] Screenshot #{shot_count[0]}: {label or page.url[:80]}")
            return path
        except Exception as e:
            print(f"[Capture] Screenshot failed: {e}")
            return None

    def _save_bundle():
        bundle = {
            "city": city,
            "booking_url": booking_url,
            "requests": requests_log,
            "responses": responses_log,
            "screenshots": screenshots,
        }
        path = f"{base}.capture.json"
        Path(path).write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[Capture] Bundle saved: {path}")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url(config))
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()

        # Scrub Playwright automation markers — same as booker.py.
        await page.add_init_script("""
            try { delete window.__playwright; } catch(e) {}
            try { delete window.__pwInitScripts; } catch(e) {}
            try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array; } catch(e) {}
            try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise; } catch(e) {}
        """)

        # ── Network capture ────────────────────────────────────────────────
        def _on_request(req):
            entry_r = {
                "ts": datetime.now().isoformat(),
                "method": req.method,
                "url": req.url,
                "post_data": req.post_data or "",
            }
            requests_log.append(entry_r)
            if req.method == "POST":
                print(f"[Capture] POST → {req.url[:80]}")

        async def _on_response(resp):
            if "as-visa.com" not in resp.url:
                return
            try:
                body = (await resp.body()).decode("utf-8", errors="replace")
            except Exception:
                body = ""
            # Keep the full body for the booking-page HTML AND for the site's JS.
            # The submit-button wiring and the "Emin misiniz?" SweetAlert flow are
            # pure client-side JS (they make no network request of their own), and
            # they usually live in an external /PageJs/*.js file rather than inline —
            # so we must retain full JS bodies to see how the automated submit works.
            # Everything else is capped so the bundle doesn't balloon.
            u = resp.url.lower()
            keep_full = (
                u.rstrip("/").endswith("bireysel-basvuru")
                or u.endswith(".js")
                or "/pagejs/" in u
            )
            entry_r = {
                "ts": datetime.now().isoformat(),
                "status": resp.status,
                "url": resp.url,
                "body": body if keep_full else body[:4000],
            }
            responses_log.append(entry_r)
            if resp.request.method == "POST":
                print(f"[Capture] Response {resp.status} ← {resp.url[:80]}")
                print(f"[Capture]   body: {body[:200]}")

        page.on("request", _on_request)
        page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

        # ── Navigate to booking page ───────────────────────────────────────
        print(f"[Capture] Navigating to {booking_url} ...")
        await page.goto(booking_url, timeout=30_000)

        send_telegram(
            f"Manual capture started — {city}\n"
            "Go ahead and fill + submit the form. Everything is being recorded."
        )
        print("\n[Capture] *** Browser is open. Do the booking manually. ***")
        print("[Capture] Press Ctrl+C here when done to save the full bundle.\n")

        # Periodic screenshot every 30s while on the booking page
        async def _periodic():
            while True:
                await asyncio.sleep(30)
                if "as-visa.com" in page.url:
                    try:
                        shot_count[0] += 1
                        path = f"{base}.shot{shot_count[0]:03d}_periodic.png"
                        await page.screenshot(path=path, full_page=False)
                        screenshots.append({"n": shot_count[0], "label": "periodic", "path": path, "url": page.url})
                    except Exception:
                        pass
                    # Snapshot the LIVE rendered form DOM while we're on the booking
                    # page. The final DOM dump only fires on close (which lands on the
                    # confirmation page), so without this we never capture the form's
                    # actual submit button / dialog markup. Overwrite one file so we
                    # always keep the most recent pre-submit form state.
                    if page.url.rstrip("/").endswith("bireysel-basvuru"):
                        try:
                            form_dom = await page.content()
                            Path(f"{base}.form_dom.html").write_text(form_dom, encoding="utf-8")
                        except Exception:
                            pass

        periodic_task = asyncio.ensure_future(_periodic())

        try:
            await page.wait_for_event("close", timeout=0)
            print("[Capture] Chrome tab closed.")
        except KeyboardInterrupt:
            pass
        except Exception:
            pass
        finally:
            periodic_task.cancel()
            # Final DOM + screenshot
            try:
                dom = await page.content()
                Path(f"{base}.final.dom.html").write_text(dom, encoding="utf-8")
            except Exception:
                pass
            await _screenshot(page, "final")
            _save_bundle()
            send_telegram(f"Manual capture complete. Bundle: capture_{city}_{Path(base).name.split('_', 2)[-1]}")


def main():
    city = sys.argv[1] if len(sys.argv) > 1 else "Istanbul"
    asyncio.run(run_capture(city))


if __name__ == "__main__":
    main()
