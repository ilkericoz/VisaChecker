import html as html_lib
import json
import re
from datetime import datetime
from pathlib import Path

from visa.config import FORM_MARKER, CSRF_MARKER

CAPTCHA_DATA_URI_RE = re.compile(
    r'<img[^>]*src="data:image/(png|jpeg|jpg|gif);base64,([^"]+)"',
    re.IGNORECASE,
)
RECAPTCHA_KEY_RE = re.compile(r"recaptchaSiteKey\s*=\s*['\"]([^'\"]+)['\"]")
# 6-digit code from enteredCode label (used in HTTP forensic dump only; booker uses OCR on live img)
ENTERED_CODE_RE = re.compile(
    r'for="enteredCode"[^>]*>.*?<span[^>]*>(\d{5,6})</span>',
    re.DOTALL | re.IGNORECASE,
)


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

        if http_session is not None:
            try:
                sp_url = "https://appointment.as-visa.com/PageJs/security-protection.js"
                sp_r = http_session.get(sp_url, timeout=10)
                if sp_r.status_code == 200 and sp_r.text.strip():
                    Path("security-protection.js").write_text(sp_r.text, encoding="utf-8")
                    written.append("security-protection.js (shared)")
            except Exception:
                pass

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
            ec = ENTERED_CODE_RE.search(html)
            if ec:
                meta["entered_code"] = ec.group(1)
        Path(f"{base}.meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written.append("meta.json")

        print(f"  Forensic bundle ({base}): {', '.join(written)}")
    except Exception as e:
        print(f"[!] Forensic dump failed: {e}")
