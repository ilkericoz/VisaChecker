# TODO / Future Improvements

## Bot Behavior
- [ ] `/screenshot` should also run availability check and alert if slot found (currently separate from detection logic)
- [ ] Re-alert every X minutes if slot is still open (in case first notification is missed)

## Auto-Book
Field names already known from page JS (obfuscated but stable):
- TC ID:       `ast_0986af`
- Name:        `asn_a347c0`
- Surname:     `assn_6fe6d9`
- Email:       `ase_1bf435`
- Passport no: `aspassno_fb4560`
- nationality, date, time fields — need to inspect makeAppointment.js / tarihGetir.js / saatGetir.js

Anti-bot obstacles to solve before auto-book works:
- [ ] Google reCAPTCHA (site key: `6Lf22HgrAAAAAP3u20U_HvrMsqmtltl7HcpezMWj`)
- [ ] Cloudflare Turnstile — harder than reCAPTCHA, likely needs CapSolver
- [ ] `formStartTime` field — they check how long the form was open, need human-like delay before submit
- [ ] `/PageJs/security-protection.js` — custom bot protection, unknown checks

## Detection
- [ ] Add secondary form-presence check as fallback (in case phrase stays in DOM but form also appears) — need real HTML from an open slot to find correct field selectors first
- [ ] If page structure changes, detection phrase may silently break — add a way to verify detection is still working (e.g. monthly test alert)

## Bot Detection / Stealth
- [ ] Add random mouse movement / scroll before checking
- [ ] Rotate user-agent strings between checks
- [ ] Add `playwright-stealth` (free, patches more fingerprint signals beyond navigator.webdriver) — `pip install playwright-stealth`, then `await stealth_async(page)` after page creation
- [ ] If actually blocked by reCAPTCHA: integrate 2captcha or CapSolver API (~$1-3/1000 solves) to auto-solve and inject token
- [ ] If still blocked: residential proxies (~$10-30/month) — last resort

## Defeating Cloudflare Turnstile (for auto-book)

How Turnstile works:
- Runs silently in background, no visible challenge
- Checks: browser fingerprint, canvas/WebGL/audio APIs, navigator.webdriver, mouse movement, scroll behavior, keystroke timing, formStartTime, IP reputation, TLS fingerprint
- Produces a cryptographic token tied to the specific browser session — can't be forged or bought from solving services like reCAPTCHA can

Options to defeat it, best to worst:

**Option 1 — Connect to user's real Chrome (best, free)**
- Playwright can attach to an already-running Chrome via remote debugging (`playwright.chromium.connect_over_cdp("http://localhost:9222")`)
- Turnstile sees a real browser with real history, cookies, fingerprint — looks 100% human
- Launch Chrome once with: `chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\ChromeBot`
- Bot detects slot → connects to real Chrome → fills form → user clicks submit (or bot does)
- This is the cleanest solution, no third-party services needed

**Option 2 — Humanized Playwright session (free, less reliable)**
- `playwright-stealth` + realistic mouse movements + random typing delays + keeping page open long enough
- Turnstile scores sessions — might pass if humanization is convincing enough
- Hit or miss, Turnstile updates frequently

**Option 3 — CapSolver (paid, ~$1-3/1000)**
- Claims to handle Turnstile but inconsistent
- Still needs a non-headless browser session to work properly

Recommendation: implement Option 1 for auto-book. Detection/checking stays headless as-is.

## Notifications
- [ ] Send a Telegram alert if the bot crashes or goes silent unexpectedly
