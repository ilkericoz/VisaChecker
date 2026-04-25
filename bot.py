"""
AS-VISA Hungary Schengen Appointment Bot — Fast HTTP Mode

Hybrid approach:
  - Playwright boots up, loads the page to get past Cloudflare, then extracts cookies.
  - requests uses those cookies for fast HTTP GET checks (~200ms each).
  - Detection is structural: the booking form (AppointmentTabID +
    __RequestVerificationToken) only renders when slots are open; the
    "no slots" block replaces it otherwise.
  - On detection, CSRF + option values are parsed straight from the HTML
    and TarihGetir is called directly to fetch actual available dates —
    no browser needed on the hot path.
  - On 403 or unknown layout, Playwright re-bootstraps the session.
  - Session is proactively refreshed every SESSION_REFRESH_INTERVAL seconds.
  - On IP ban (connection refused), sends Telegram alert and pauses until /resume.

Telegram commands:
  /screenshot          — take fresh screenshots of both pages and send them
  /status              — show check count, last check time, next check ETA, mode
  /fast                — switch to 30-60s interval
  /normal              — switch back to default interval
  /interval <min> <max> — set custom interval in minutes, e.g. /interval 1 5
  /resume              — resume after IP ban (restart modem first, or auto-resumes after 15 min)
  /compare             — fetch each page via HTTP and Playwright, compare results
  /probe               — blindly POST to TarihGetir with tabId 1-15 to discover valid IDs
  /extract             — load each page in Playwright and scrape form field values (only useful when slots are open)
  /book                — manually run the fast-track booker against the most recent detected slot

Config flags:
  auto_book            — auto-launch fast-track booker on slot detection (default false; flip when ready)
  headed_on_book       — show the booking browser window (default true; required for fast-track captcha)
"""

import asyncio
import html as html_lib
import json
import os
import random
import re
import time
import winsound
from datetime import datetime, date
from pathlib import Path

import requests as req_lib
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from plyer import notification

load_dotenv()

HEARTBEAT_INTERVAL = 6 * 60 * 60      # 6 hours
SESSION_REFRESH_INTERVAL = 15 * 60    # re-bootstrap Playwright session every 15 min
IP_BAN_AUTO_RESUME_AFTER = 15 * 60   # auto-resume after 15 min if still banned
SNAPSHOTS_DIR = Path("snapshots")
SANITY_PHRASE = "Randevu"
FORM_MARKER = 'id="AppointmentTabID"'
CSRF_MARKER = '__RequestVerificationToken'
TARIH_GETIR_BASE = "https://appointment.as-visa.com"
BOOKING_PROFILE_PATH = "booking_profile.json"

# Form fields whose `name` rotates every page render (anti-scraper).
# Map by the placeholder text (which IS stable) → profile key.
ROTATING_FIELD_PLACEHOLDERS = {
    "Adınızı Giriniz":         "first_name",
    "Soyadınızı Giriniz":      "last_name",
    "Pasaport No Giriniz":     "passport_no",
    "T.C. Kimlik No Giriniz":  "tc_kimlik",
}
# Static (non-rotating) text inputs: selector → profile key
STATIC_TEXT_FIELDS = {
    'input[name="reTCKN"]':        "tc_kimlik_confirm",
    'input[name="DogumYili"]':     "birth_year",
    'input[name="Phone"]':         "phone",
    'input[name="reEmail"]':       "email_confirm",
    'input#passportEndDate':       "passport_expiry",
    'input#TravelDate':            "travel_date",  # injected as dd/mm/yyyy via JS
}
# Static <select> fields: selector → profile key
STATIC_SELECT_FIELDS = {
    'select#NationalityTabID': "nationality",
    'select#AppointmentTabID': "appointment_type",
    'select#TravelSubject':    "travel_purpose",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class IPBannedError(Exception):
    pass


def load_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Forensic dump — when slots are detected, save everything we have for replay
# ---------------------------------------------------------------------------

CAPTCHA_DATA_URI_RE = re.compile(
    r'<img[^>]*src="data:image/(png|jpeg|jpg|gif);base64,([^"]+)"',
    re.IGNORECASE,
)
RECAPTCHA_KEY_RE = re.compile(r"recaptchaSiteKey\s*=\s*['\"]([^'\"]+)['\"]")


def extract_inline_captcha(html):
    """Return (ext, bytes) for the embedded KCAPTCHA image, or (None, None).
    The server HTML-entity-encodes '+' as '&#x2B;' (and similar for '/', '='),
    so we unescape before base64-decoding.
    """
    import base64
    for m in CAPTCHA_DATA_URI_RE.finditer(html):
        # Skip favicons/logos by requiring an alt hint near the tag — the
        # security-code img has alt="Güvenlik Kodu". If no alt match found,
        # fall through to the first hit (better than nothing for forensics).
        tag_end = html.find('>', m.start())
        tag = html[m.start():tag_end + 1] if tag_end != -1 else ''
        is_captcha = 'üvenlik' in tag or 'uvenlik' in tag or 'aptcha' in tag.lower()
        if not is_captcha and CAPTCHA_DATA_URI_RE.search(html, tag_end):
            continue  # there are more imgs to try
        b64 = html_lib.unescape(m.group(2))
        try:
            return m.group(1), base64.b64decode(b64)
        except Exception:
            continue
    return None, None


def dump_forensic_bundle(base, *, http_session=None, html=None, dom_html=None,
                         tarih_results=None, status_code=None, elapsed=None,
                         detector=None):
    """
    On slot detection, write everything useful for post-mortem and auto-book replay:
      {base}.html         raw HTTP response body that triggered detection
      {base}.dom.html     Playwright-rendered DOM (only on PW path)
      {base}.cookies.json session cookies that successfully fetched the form
      {base}.captcha.{ext} embedded KCAPTCHA image bytes
      {base}.tarih.json   per-AppointmentTabID date results from TarihGetir
      {base}.meta.json    status, elapsed ms, detector path, recaptcha key, etc.
    """
    written = []
    try:
        if html is not None:
            Path(f"{base}.html").write_text(html, encoding="utf-8")
            written.append("html")

        if dom_html is not None and dom_html != html:
            Path(f"{base}.dom.html").write_text(dom_html, encoding="utf-8")
            written.append("dom.html")

        if http_session is not None:
            cookies = {c.name: c.value for c in http_session.cookies}
            Path(f"{base}.cookies.json").write_text(
                json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            written.append("cookies.json")

        if html is not None:
            ext, blob = extract_inline_captcha(html)
            if blob:
                Path(f"{base}.captcha.{ext}").write_bytes(blob)
                written.append(f"captcha.{ext}")

        if tarih_results is not None:
            Path(f"{base}.tarih.json").write_text(
                json.dumps(tarih_results, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            written.append("tarih.json")

        meta = {
            "timestamp": datetime.now().isoformat(),
            "detector": detector,
            "status_code": status_code,
            "elapsed_ms": int(elapsed * 1000) if elapsed is not None else None,
            "has_form_marker": bool(html and FORM_MARKER in html),
            "has_csrf_marker": bool(html and CSRF_MARKER in html),
            "recaptcha_site_key": None,
        }
        if html:
            rk = RECAPTCHA_KEY_RE.search(html)
            if rk:
                meta["recaptcha_site_key"] = rk.group(1)
        Path(f"{base}.meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written.append("meta.json")

        print(f"  Forensic bundle ({base}): {', '.join(written)}")
    except Exception as e:
        print(f"[!] Forensic dump failed: {e}")


# ---------------------------------------------------------------------------
# Fast-track booker — headed browser, auto-fill personal info, stop at captcha
# ---------------------------------------------------------------------------

def load_booking_profile():
    try:
        with open(BOOKING_PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[!] Booking profile load failed: {e}")
        return None


def pick_date(tarih_results, preference):
    """
    Pick an AppointmentDate from TarihGetir results based on profile preference.
    TarihGetir returns dates like '2026-4-15'; normalize to YYYY-MM-DD before comparing.
    Returns (option_label, normalized_date) or (None, None).
    """
    if not tarih_results or not tarih_results.get("results"):
        return None, None
    rule = (preference or {}).get("rule", "first_available")
    earliest = (preference or {}).get("earliest_date")
    latest = (preference or {}).get("latest_date")
    for opt in tarih_results["results"]:
        for d in opt.get("dates", []):
            try:
                p = d.split("-")
                norm = f"{int(p[0]):04d}-{int(p[1]):02d}-{int(p[2]):02d}"
            except Exception:
                continue
            if rule == "window":
                if earliest and norm < earliest:
                    continue
                if latest and norm > latest:
                    continue
            return opt.get("label"), norm
    return None, None


async def fast_track_book(entry, http_session, profile, tarih_results, base, config):
    """
    Open a HEADED Playwright window, fill all personal-info fields from
    the profile, and stop. User completes captcha + date/time + submit.
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

    headless = not config.get("headed_on_book", True)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(user_agent=USER_AGENT)
            cookie_list = []
            for c in http_session.cookies:
                cookie_list.append({
                    "name": c.name, "value": c.value,
                    "domain": c.domain or "appointment.as-visa.com",
                    "path": c.path or "/",
                })
            try:
                if cookie_list:
                    await context.add_cookies(cookie_list)
            except Exception as e:
                print(f"[Booker] cookie inject failed: {e}")

            page = await context.new_page()
            await page.goto(entry["url"], timeout=30_000)
            try:
                await page.wait_for_selector("#apForm", timeout=15_000)
            except Exception:
                send_telegram(
                    "Fast-track: form didn't render within 15s — slot may have closed. "
                    "Window left open so you can investigate."
                )
                return

            filled, failed = [], []

            # Rotating fields by placeholder text
            for placeholder, key in ROTATING_FIELD_PLACEHOLDERS.items():
                value = profile.get(key, "")
                if not value:
                    continue
                sel = f'input[placeholder="{placeholder}"]'
                try:
                    await page.fill(sel, str(value))
                    filled.append(key)
                except Exception as e:
                    failed.append(f"{key}: {e}")

            # Email pair: two inputs share placeholder "E-posta Giriniz".
            # Primary = rotating ase_<hex>; confirm = static reEmail.
            try:
                em = profile.get("email", "")
                if em:
                    await page.evaluate(
                        "(v) => {"
                        "  const inputs = document.querySelectorAll('input[placeholder=\"E-posta Giriniz\"]');"
                        "  for (const i of inputs) {"
                        "    if (i.name && i.name.startsWith('ase_')) {"
                        "      i.value = v;"
                        "      i.dispatchEvent(new Event('input',{bubbles:true}));"
                        "    }"
                        "  }"
                        "}",
                        em,
                    )
                    filled.append("email")
            except Exception as e:
                failed.append(f"email: {e}")

            # Static text fields (handle readonly date inputs via JS)
            DATE_KEYS = {"passport_expiry", "travel_date"}
            for sel, key in STATIC_TEXT_FIELDS.items():
                value = profile.get(key, "")
                if not value:
                    continue
                try:
                    # Date picker fields need dd/mm/yyyy not YYYY-MM-DD
                    if key in DATE_KEYS:
                        y, m, d = str(value).split("-")
                        value = f"{int(d):02d}/{int(m):02d}/{y}"
                    is_readonly = await page.eval_on_selector(sel, "el => el.readOnly")
                    if is_readonly:
                        await page.evaluate(
                            "(args) => { const el = document.querySelector(args.sel);"
                            " if (el) { el.value = args.v; el.dispatchEvent(new Event('change',{bubbles:true})); } }",
                            {"sel": sel, "v": str(value)},
                        )
                    else:
                        await page.fill(sel, str(value))
                    filled.append(key)
                except Exception as e:
                    failed.append(f"{key}: {e}")

            # Static selects
            for sel, key in STATIC_SELECT_FIELDS.items():
                value = profile.get(key, "")
                if not value:
                    continue
                try:
                    await page.select_option(sel, value)
                    filled.append(key)
                except Exception as e:
                    failed.append(f"{key}: {e}")

            # Prefill appointment date + fetch and inject time slots.
            times = []
            if picked_date:
                try:
                    # Format datepicker expects dd/mm/yyyy
                    y, mo, d = picked_date.split("-")
                    date_for_picker = f"{int(d):02d}/{int(mo):02d}/{y}"
                    await page.evaluate(
                        "(v) => { const el = document.getElementById('datepicker');"
                        " if (el) { el.value = v; el.dispatchEvent(new Event('change',{bubbles:true})); } }",
                        date_for_picker,
                    )
                except Exception:
                    pass

                # Fetch available time slots via SaatGetir and inject into select
                try:
                    times = await asyncio.get_event_loop().run_in_executor(
                        None, fetch_available_times, http_session, entry, picked_date
                    )
                    if times:
                        await page.evaluate(
                            "(slots) => {"
                            "  const sel = document.getElementById('AppointmentTime');"
                            "  if (!sel) return;"
                            "  sel.innerHTML = '';"
                            "  for (const s of slots) {"
                            "    const o = document.createElement('option');"
                            "    o.value = s.value; o.textContent = s.text;"
                            "    sel.appendChild(o);"
                            "  }"
                            "  if (slots.length > 0) {"
                            "    sel.value = slots[0].value;"
                            "    sel.dispatchEvent(new Event('change',{bubbles:true}));"
                            "  }"
                            "}",
                            times,
                        )
                        filled.append(f"appointment_time ({times[0].get('text','')})")
                except Exception as e:
                    failed.append(f"appointment_time: {e}")

            # Persist booker outcome to forensics
            try:
                Path(f"{base}.booker.json").write_text(
                    json.dumps({
                        "filled": filled, "failed": failed,
                        "picked_date": picked_date, "label": label,
                        "time_slots": times,
                    }, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

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
                + ".\nWindow is open — type the captcha + click Randevu Al."
                + time_line
                + (f"\nFailed: {'; '.join(failed[:5])}" if failed else "")
            )

            # Hold the booker open until the user closes the page manually.
            try:
                await page.wait_for_event("close", timeout=0)
            except Exception:
                pass

    except Exception as e:
        send_telegram(f"Fast-track booker crashed: {e}")
        print(f"[Booker] crashed: {e}")


# ---------------------------------------------------------------------------
# Session management — Playwright bootstraps, requests checks fast
# ---------------------------------------------------------------------------

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

            # Extract cookies from Playwright context and put them in requests session
            cookies = await page.context.cookies()
            for c in cookies:
                http_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        except Exception as e:
            print(f"  [Session] Bootstrap failed for {entry['name']}: {e}")

    print(f"  [Session] Got {len(http_session.cookies)} cookies from Playwright")
    return http_session


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


def fetch_available_dates(http_session, entry, csrf, tab_id, country_id):
    """
    POST to TarihGetir with the CSRF token + selected tab/country IDs.
    Returns list of date strings (e.g. ["2026-4-15", ...]) or [].
    """
    api_path = entry.get("tarih_getir_path")
    if not api_path:
        return []
    url = TARIH_GETIR_BASE + api_path
    headers = {
        "RequestVerificationToken": csrf or "",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": entry["url"],
    }
    try:
        r = http_session.post(
            url,
            data={"tabId": tab_id, "countryid": country_id},
            headers=headers,
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  [TarihGetir] {entry['name']} failed: {e}")
        return []


def fetch_available_times(http_session, entry, date_norm):
    """
    POST to SaatGetir for a given date.
    date_norm is YYYY-MM-DD; SaatGetir expects dd/mm/yyyy (datepicker format).
    Returns list of {value, text} dicts or [].
    """
    api_path = entry.get("saat_getir_path")
    if not api_path:
        return []
    try:
        y, m, d = date_norm.split("-")
        date_tab = f"{int(d):02d}/{int(m):02d}/{y}"
    except Exception:
        return []
    url = TARIH_GETIR_BASE + api_path
    try:
        r = http_session.post(
            url,
            data={"dateTab": date_tab},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": entry["url"]},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  [SaatGetir] {entry['name']} failed: {e}")
        return []


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


def fast_check(http_session, entry, no_appt_phrase):
    """
    Fast HTTP GET check using the shared requests session.
    Detection is structural: the booking form (AppointmentTabID +
    __RequestVerificationToken) only renders when slots are open. The
    "no slots" block and the form are mutually exclusive in the server
    response.
    Returns (available: bool, html: str, elapsed: float, status_code: int)
    or raises on failure.
    Raises IPBannedError on connection refused (Cloudflare IP ban).
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


# ---------------------------------------------------------------------------
# Telegram command listener
# ---------------------------------------------------------------------------

async def telegram_command_listener(state, page_lock, page, config):
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
                    lines = []
                    for entry in urls:
                        # HTTP fetch
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

                        # Playwright fetch
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
                    base = "https://appointment.as-visa.com"
                    for entry in urls:
                        api_path = entry.get("tarih_getir_path")
                        if not api_path:
                            send_telegram(f"{entry['name']}: no tarih_getir_path in config")
                            continue

                        # Try to grab CSRF from page via Playwright
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

                        api_url = base + api_path
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

                        # Send per-city to avoid Telegram message length limits
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run():
    config = load_config()
    urls = config["urls"]
    no_appt_phrase = config["no_appointment_phrase"]

    print("Visa Appointment Bot — Fast HTTP Mode")
    print(f"Checking every {config['check_interval_min_seconds']//60}-{config['check_interval_max_seconds']//60} min")
    print(f"Cities: {', '.join(e['name'] for e in urls)}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config["headless"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

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
                                async def _book_then_clear(entry, http_session, profile, tarih_results, base, config):
                                    try:
                                        await fast_track_book(entry, http_session, profile, tarih_results, base, config)
                                    finally:
                                        state["booking_in_progress"] = False
                                asyncio.create_task(_book_then_clear(entry, http_session, profile, tarih_results, base, config))

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
                                        async def _book_then_clear(entry, http_session, profile, tarih_results, base, config):
                                            try:
                                                await fast_track_book(entry, http_session, profile, tarih_results, base, config)
                                            finally:
                                                state["booking_in_progress"] = False
                                        asyncio.create_task(_book_then_clear(entry, http_session, profile, tarih_results, base, config))

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


if __name__ == "__main__":
    asyncio.run(run())
