import asyncio
import os
import re

from visa.telegram import send_telegram


async def solve_entered_code(page):
    """
    OCR the enteredCode CAPTCHA image via ddddocr.
    Returns (code_str, None) on success, (None, error_detail) on failure.
    """
    try:
        import base64 as _b64
        import html as _html_mod
        import ddddocr as _ddddocr

        img_src = await page.evaluate(
            "() => { const img = document.querySelector('label[for=\"enteredCode\"] img');"
            " return img ? img.src : null; }"
        )
        if not (img_src and ',' in img_src):
            return None, f"OCR got None from img_src={'missing'}"
        _, b64data = img_src.split(',', 1)
        png_bytes = _b64.b64decode(_html_mod.unescape(b64data))
        _ocr = _ddddocr.DdddOcr(show_ad=False)
        raw = _ocr.classification(png_bytes)
        code = ''.join(c for c in raw if c.isdigit())[:6]
        if re.match(r'^\d{5,6}$', code):
            return code, None
        return None, f"OCR got {code!r} from img_src='present'"
    except Exception as e:
        return None, str(e)


async def solve_turnstile(page):
    """
    Poll for Cloudflare Turnstile auto-completion for 20s.
    Falls back to CapSolver if not completed.
    Returns token string or empty string.
    """
    cf_val = ""
    for _ in range(40):
        try:
            cf_val = await page.evaluate("() => document.getElementById('cfToken')?.value || ''")
        except Exception:
            pass
        if cf_val:
            return cf_val
        await asyncio.sleep(0.5)

    # CapSolver fallback
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")
    if capsolver_key:
        try:
            send_telegram("Fast-track: Turnstile didn't auto-complete — calling CapSolver...")
            import capsolver as _capsolver
            _capsolver.api_key = capsolver_key
            ts_sitekey = await page.evaluate(
                "() => document.querySelector('.cf-turnstile')?.dataset?.sitekey || ''"
            ) or "0x4AAAAAABdidRUErm8HlBu9"
            solution = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _capsolver.solve({
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": page.url,
                    "websiteKey": ts_sitekey,
                })
            )
            cf_val = solution.get("token", "")
            if cf_val:
                await page.evaluate(
                    "(t) => { const el = document.getElementById('cfToken'); if (el) el.value = t; }",
                    cf_val,
                )
                send_telegram("Fast-track: CapSolver Turnstile token injected.")
        except Exception as _ce:
            send_telegram(f"Fast-track: CapSolver failed: {_ce}")
    else:
        send_telegram("Fast-track: CAPSOLVER_API_KEY not set — skipping CapSolver fallback.")

    return cf_val
