import asyncio
import os
import re

from visa.telegram import send_telegram


async def solve_entered_code(page):
    """
    Read the 6-digit enteredCode verification code from the page.
    Both cities now render it as plain text in a <span> inside the label.
    Old Ankara used a base64 PNG image — OCR fallback kept for safety.
    Returns (code_str, None) on success, (None, error_detail) on failure.
    """
    # Plain-text span (Istanbul + new Ankara)
    try:
        code = await page.evaluate(
            "() => {"
            "  const label = document.querySelector('label[for=\"enteredCode\"]');"
            "  if (!label) return null;"
            "  for (const span of label.querySelectorAll('span')) {"
            "    const t = span.textContent.replace(/\\D/g, '').slice(0, 6);"
            "    if (t.length >= 5) return t;"
            "  }"
            "  return null;"
            "}"
        )
        if code:
            return code, None
    except Exception:
        pass

    # Base64 PNG fallback (old Ankara form — kept in case it returns)
    try:
        import base64 as _b64
        import html as _html_mod
        import ddddocr as _ddddocr

        img_src = await page.evaluate(
            "() => { const img = document.querySelector('label[for=\"enteredCode\"] img');"
            " return img ? img.src : null; }"
        )
        if not (img_src and ',' in img_src):
            return None, "no span text and no img found — label not rendered yet or selector changed"
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


async def solve_recaptcha(page):
    """
    Solve Google reCAPTCHA v2 via CapSolver if the page has window.recaptchaSiteKey.
    Returns token string or empty string.
    Istanbul has reCAPTCHA v2 in addition to Turnstile; Ankara does not.
    """
    sitekey = ""
    try:
        sitekey = await page.evaluate("() => window.recaptchaSiteKey || ''")
    except Exception:
        pass
    if not sitekey:
        return ""

    # Already solved (unlikely on first load, but check)
    try:
        existing = await page.evaluate("() => document.getElementById('recaptchaToken')?.value || ''")
        if existing:
            return existing
    except Exception:
        pass

    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")
    if not capsolver_key:
        send_telegram("Fast-track: reCAPTCHA required but CAPSOLVER_API_KEY not set.")
        return ""

    try:
        send_telegram("Fast-track: reCAPTCHA detected — calling CapSolver...")
        import capsolver as _capsolver
        _capsolver.api_key = capsolver_key
        # api.js?render=KEY means v3 (score-based, invisible). Fall back to v2 if v3 fails.
        is_v3 = bool(await page.evaluate(
            "() => !document.querySelector('.g-recaptcha')"
        ))
        task = {
            "type": "ReCaptchaV3TaskProxyLess" if is_v3 else "ReCaptchaV2TaskProxyLess",
            "websiteURL": page.url,
            "websiteKey": sitekey,
        }
        if is_v3:
            task["pageAction"] = "submit"
            task["minScore"] = 0.5
        solution = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _capsolver.solve(task)
        )
        token = solution.get("gRecaptchaResponse", "")
        if token:
            await page.evaluate(
                "(t) => {"
                "  const set = (el) => { if (el) { el.value = t;"
                "    el.dispatchEvent(new Event('change', { bubbles: true })); } };"
                "  set(document.getElementById('recaptchaToken'));"
                "  set(document.querySelector('textarea#g-recaptcha-response'));"
                "  set(document.querySelector('textarea[name=\"g-recaptcha-response\"]'));"
                "}",
                token,
            )
            send_telegram(f"Fast-track: CapSolver reCAPTCHA {'v3' if is_v3 else 'v2'} token injected.")
        return token
    except Exception as _ce:
        send_telegram(f"Fast-track: CapSolver reCAPTCHA failed: {_ce}")
        return ""


async def solve_turnstile(page):
    """
    Poll for Cloudflare Turnstile auto-completion for 20s.
    Falls back to CapSolver if not completed.
    Returns token string or empty string.
    """
    # Turnstile auto-completion populates the widget's own `cf-turnstile-response`
    # field; the site's onTurnstileSuccess callback then mirrors it into `cfToken`.
    # Poll BOTH so a slow/renamed callback doesn't make us miss a real solve — if
    # only the standard field is set, copy it into cfToken before returning.
    cf_val = ""
    for _ in range(40):
        try:
            cf_val = await page.evaluate(
                "() => {"
                "  const a = document.getElementById('cfToken')?.value || '';"
                "  if (a) return a;"
                "  const b = document.querySelector('input[name=\"cf-turnstile-response\"]')?.value || '';"
                "  if (b) {"
                "    const el = document.getElementById('cfToken');"
                "    if (el && !el.value) el.value = b;"
                "    return b;"
                "  }"
                "  return '';"
                "}"
            )
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
                # A real Turnstile solve does two things the site relies on:
                #   1. the widget fills its own `cf-turnstile-response` hidden input
                #   2. the data-callback `onTurnstileSuccess(token)` runs, which is
                #      the site's JS that populates `cfToken`.
                # Injecting only into cfToken skips both the standard response field
                # (which the server validates) and any side effects of the callback,
                # so we replicate all three.
                await page.evaluate(
                    "(t) => {"
                    "  const set = (el) => { if (el) { el.value = t;"
                    "    el.dispatchEvent(new Event('change', { bubbles: true })); } };"
                    "  set(document.getElementById('cfToken'));"
                    "  set(document.querySelector('input[name=\"cf-turnstile-response\"]'));"
                    "  try { if (typeof onTurnstileSuccess === 'function') onTurnstileSuccess(t); }"
                    "  catch (e) {}"
                    "}",
                    cf_val,
                )
                send_telegram("Fast-track: CapSolver Turnstile token injected "
                              "(cfToken + cf-turnstile-response + callback).")
        except Exception as _ce:
            send_telegram(f"Fast-track: CapSolver failed: {_ce}")
    else:
        send_telegram("Fast-track: CAPSOLVER_API_KEY not set — skipping CapSolver fallback.")

    return cf_val
