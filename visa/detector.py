import asyncio
import html as html_lib
import re
import time
from datetime import datetime, date
from pathlib import Path

# Plain requests, deliberately. We tested curl_cffi (impersonate=chrome) to get a
# browser JA3 — the appointment server actively DROPS those connections ("empty
# reply from server") while answering plain requests with a clean 200, and the
# polling path doesn't even need a cf_clearance cookie. So requests is correct here.
import requests as req_lib
import winsound
from plyer import notification

from visa.config import (
    SANITY_PHRASE, FORM_MARKER, CSRF_MARKER, SNAPSHOTS_DIR, USER_AGENT,
)
from visa.telegram import send_telegram, send_telegram_photo
from visa.date_api import fetch_available_dates


def notify(city, url, method="HTTP"):
    send_telegram(f"RANDEVU ACILDI — {city}\nYontem: {method}\n{url}")

    try:
        notification.notify(
            title=f"Vize Randevusu Acildi! - {city}",
            message=f"{city} icin randevu acildi ({method}). Hemen rezervasyon yap!",
            app_name="Visa Bot",
            timeout=30,
        )
    except Exception as e:
        print(f"[!] Desktop notification failed: {e}")

    for _ in range(10):
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        time.sleep(0.3)

    print(f"\n{'='*60}")
    print(f"  RANDEVU ACILDI! - {city}  [{method}]")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {url}")
    print(f"{'='*60}\n")


def save_daily_snapshot(city, html):
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = SNAPSHOTS_DIR / f"{city}_{date.today()}.html"
    if not path.exists():
        path.write_text(html, encoding="utf-8")
        print(f"  Snapshot saved: {path}")


async def take_and_send_screenshot(page, base, caption):
    try:
        await page.screenshot(path=f"{base}.png", full_page=True)
        print(f"  Screenshot: {base}.png")
        send_telegram_photo(f"{base}.png", caption=caption)
    except Exception as e:
        print(f"[!] Screenshot/send failed: {e}")


def fast_check(http_session, entry, no_appt_phrase):
    """
    Fast HTTP GET check using the shared requests session.
    Detection is structural: the booking form (AppointmentTabID +
    __RequestVerificationToken) only renders when slots are open. The
    "no slots" block and the form are mutually exclusive in the server response.
    Returns (available: bool, html: str, elapsed: float, status_code: int)
    or raises on failure.
    """
    t0 = time.time()
    try:
        r = http_session.get(entry["url"], timeout=15)
    except req_lib.exceptions.ConnectionError as e:
        raise RuntimeError(f"Connection error — {e}") from e

    elapsed = time.time() - t0

    if r.status_code == 403:
        raise RuntimeError("403 — session expired, need Playwright refresh")

    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")

    html = r.text

    if SANITY_PHRASE not in html:
        raise RuntimeError(f"Sanity check failed: '{SANITY_PHRASE}' not found")

    has_form = FORM_MARKER in html and CSRF_MARKER in html
    has_no_slots = no_appt_phrase in html

    if has_form:
        available = True
    elif has_no_slots:
        available = False
    else:
        # Neither marker — page layout may have shifted. Treat as sanity fail
        # so we fall back to Playwright and surface the issue instead of
        # silently misreporting.
        raise RuntimeError(
            "Layout unknown: neither form nor no-slots block present"
        )

    return available, html, elapsed, r.status_code


async def bootstrap_session(page, urls):
    """
    Load each URL in Playwright to get past Cloudflare, then extract cookies.
    Returns a requests.Session ready to use with those cookies.
    """
    print("  [Session] Bootstrapping session via Playwright...")
    http_session = req_lib.Session()
    http_session.headers.update({"User-Agent": USER_AGENT})

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
            # Wait for auto-closing popup
            await asyncio.sleep(5)

            cookies = await page.context.cookies()
            for c in cookies:
                http_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        except Exception as e:
            print(f"  [Session] Bootstrap failed for {entry['name']}: {e}")

    print(f"  [Session] Got {len(http_session.cookies)} cookies from Playwright")
    return http_session


def parse_form_from_html(html):
    """
    Regex-extract CSRF token + AppointmentTabID options + default NationalityTabID
    from the HTML we already fetched. No browser needed.
    Returns dict or {} if the form isn't in the HTML.
    """
    if FORM_MARKER not in html or CSRF_MARKER not in html:
        return {}

    csrf = None
    m = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html
    )
    if not m:
        m = re.search(
            r'value="([^"]+)"[^>]*name="__RequestVerificationToken"', html
        )
    if m:
        csrf = m.group(1)

    appt_options = []
    appt_block = re.search(
        r'<select[^>]*id="AppointmentTabID"[^>]*>(.*?)</select>', html, re.DOTALL
    )
    if appt_block:
        for om in re.finditer(
            r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>',
            appt_block.group(1), re.DOTALL,
        ):
            val = om.group(1).strip()
            text = html_lib.unescape(re.sub(r'\s+', ' ', om.group(2))).strip()
            if val:
                appt_options.append({"value": val, "text": text})

    nat_default = None
    nat_block = re.search(
        r'<select[^>]*id="NationalityTabID"[^>]*>(.*?)</select>', html, re.DOTALL
    )
    if nat_block:
        # Prefer the option with `selected`, else first non-empty value
        sel = re.search(
            r'<option[^>]*value="([^"]+)"[^>]*selected', nat_block.group(1)
        )
        if sel:
            nat_default = sel.group(1)
        else:
            first = re.search(
                r'<option[^>]*value="([^"]+)"', nat_block.group(1)
            )
            if first:
                nat_default = first.group(1)

    return {"csrf": csrf, "appt_options": appt_options, "nat_default": nat_default}


async def extract_form_fields(page, entry):
    """
    Load the appointment page in Playwright and scrape select options + CSRF token.
    Only useful when slots are actually open (form is rendered).
    Sends findings to Telegram.
    """
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

        csrf = None
        try:
            csrf = await page.eval_on_selector(
                'input[name="__RequestVerificationToken"]', "el => el.value"
            )
        except Exception:
            pass

        appt_options = []
        try:
            appt_options = await page.evaluate(
                "() => Array.from(document.querySelectorAll('#AppointmentTabID option'))"
                ".map(o => ({value: o.value, text: o.textContent.trim()}))"
            )
        except Exception:
            pass

        nat_options = []
        try:
            nat_options = await page.evaluate(
                "() => Array.from(document.querySelectorAll('#NationalityTabID option'))"
                ".map(o => ({value: o.value, text: o.textContent.trim()}))"
            )
        except Exception:
            pass

        lines = [f"Form fields — {entry['name']}:"]
        lines.append(f"CSRF token: {'found (' + csrf[:20] + '...)' if csrf else 'NOT FOUND (form not rendered?)'}")
        if appt_options:
            lines.append("AppointmentTabID options:")
            for o in appt_options:
                lines.append(f"  value={o['value']!r}  text={o['text']!r}")
        else:
            lines.append("AppointmentTabID: not found")
        if nat_options:
            lines.append("NationalityTabID options (first 8):")
            for o in nat_options[:8]:
                lines.append(f"  value={o['value']!r}  text={o['text']!r}")
        else:
            lines.append("NationalityTabID: not found")

        send_telegram("\n".join(lines))
        return {"csrf": csrf, "appt_options": appt_options, "nat_options": nat_options}

    except Exception as e:
        send_telegram(f"extract_form_fields failed ({entry['name']}): {e}")
        return {}


async def report_dates_from_html(http_session, entry, html, loop):
    """
    Parse CSRF + option values from the HTML, hit TarihGetir for each
    AppointmentTabID option, Telegram the date lists. All HTTP — no browser.
    Returns a dict with the per-option results (saved as forensic .tarih.json),
    or None if the form couldn't be parsed.
    """
    parsed = parse_form_from_html(html)
    if not parsed or not parsed.get("csrf"):
        send_telegram(
            f"{entry['name']}: slots detected but couldn't parse form from HTML "
            f"(CSRF/options missing). Try /extract or open the page manually."
        )
        return None

    csrf = parsed["csrf"]
    nat = parsed.get("nat_default") or "TÜRKİYE"
    opts = parsed.get("appt_options") or []
    if not opts:
        send_telegram(
            f"{entry['name']}: SLOTS OPEN but AppointmentTabID options not found in HTML."
        )
        return {"csrf": csrf, "nat": nat, "options": [], "results": []}

    results = []
    lines = [f"{entry['name']} — available dates:"]
    for opt in opts:
        dates = await loop.run_in_executor(
            None,
            fetch_available_dates,
            http_session, entry, csrf, opt["value"], nat,
        )
        label = opt["text"] or opt["value"]
        results.append({"value": opt["value"], "label": label, "dates": dates})
        if dates:
            lines.append(f"• {label}: {', '.join(dates)}")
        else:
            lines.append(f"• {label}: (none / API returned empty)")
    send_telegram("\n".join(lines))
    return {"csrf": csrf, "nat": nat, "options": opts, "results": results}
