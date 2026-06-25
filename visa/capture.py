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
            entry_r = {
                "ts": datetime.now().isoformat(),
                "status": resp.status,
                "url": resp.url,
                "body": body[:4000],
            }
            responses_log.append(entry_r)
            if resp.request.method == "POST":
                print(f"[Capture] Response {resp.status} ← {resp.url[:80]}")
                print(f"[Capture]   body: {body[:200]}")

        page.on("request", _on_request)
        page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

        # Screenshot on every navigation
        async def _on_load():
            label = "load"
            url = page.url
            if "as-visa.com" in url:
                label = url.split("/")[-1] or "root"
            path = await _screenshot(page, label)
            if path:
                send_telegram_photo(path, caption=f"Page loaded: {url[:80]}")

        page.on("load", lambda: asyncio.ensure_future(_on_load()))

        # Screenshot SweetAlert dialogs the moment they appear
        async def _on_dialog_appear():
            await asyncio.sleep(0.3)
            path = await _screenshot(page, "dialog")
            if path:
                try:
                    text = await page.evaluate(
                        "() => document.querySelector('.swal2-content, .swal2-html-container')?.innerText || ''"
                    )
                    send_telegram_photo(path, caption=f"Dialog: {text[:200]}")
                except Exception:
                    send_telegram_photo(path, caption="Dialog appeared")

        await page.expose_function(
            "__capture_dialog_notify",
            lambda: asyncio.ensure_future(_on_dialog_appear()),
        )
        await page.add_init_script("""
            const _origSwal = window.Swal;
            Object.defineProperty(window, 'Swal', {
                set(v) { _origSwal = v; },
                get() {
                    if (!_origSwal) return _origSwal;
                    return new Proxy(_origSwal, {
                        get(target, prop) {
                            const orig = target[prop];
                            if (prop === 'fire' && typeof orig === 'function') {
                                return function(...args) {
                                    if (window.__capture_dialog_notify) window.__capture_dialog_notify();
                                    return orig.apply(target, args);
                                };
                            }
                            return orig;
                        }
                    });
                }
            });
        """)

        # ── Wait for user to navigate to booking page ─────────────────────
        # We do NOT navigate automatically — Cloudflare trusts the user's
        # real browsing but blocks CDP-driven navigation on a cold profile.
        # Open a blank tab and let the user go there themselves.
        print(f"\n[Capture] *** Chrome is open. ***")
        print(f"[Capture] Navigate yourself to:")
        print(f"[Capture]   {booking_url}")
        print(f"[Capture] Waiting for you to land on the page...")

        # Wait until the page URL contains the booking domain
        while True:
            try:
                if "as-visa.com" in page.url:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        print(f"[Capture] Detected! Recording from here. Fill and submit the form.")
        send_telegram(
            f"Manual capture started — {city}\n"
            "Go ahead and fill + submit the form. Everything is being recorded."
        )
        print("\n[Capture] *** Do the booking manually. ***")
        print("[Capture] Press Ctrl+C here when done to save the full bundle.\n")

        # Periodic screenshot every 30s while on the booking page
        async def _periodic():
            while True:
                await asyncio.sleep(30)
                if "as-visa.com" in page.url:
                    await _screenshot(page, "periodic")

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
