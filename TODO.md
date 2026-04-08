# TODO / Future Improvements

## Bot Behavior
- [ ] `/screenshot` should also run availability check and alert if slot found (currently separate from detection logic)
- [ ] Re-alert every X minutes if slot is still open (in case first notification is missed)

## Auto-Book

### Ankara (simpler — start here)
Full source extracted to `Ankara source files/`. Key findings:
- **Plain field names**: TcKimlikNo, Name, Surname, Email, PassaportNumber, reTCKN, reEmail, DogumYili, Phone
- **No reCAPTCHA v3** — only Cloudflare Turnstile (and cfToken check is COMMENTED OUT in JS, but server may still validate)
- **No security-protection.js** — no DevTools detection or keyboard blocking
- **No FormNonce** — has `verificationCodeServer` instead (server-side code verification)
- **17-day gap rule** (stricter than Istanbul's 15-day)
- Submit endpoint: `POST /tr/ankara-bireysel-basvuru`
- Date API: `POST /AnBir/Macaristan/TarihGetir` with `{ tabId, countryid }` + `RequestVerificationToken` header
- Time API: `POST /AnBir/Macaristan/SaatGetir` with `{ dateTab: "dd/mm/yyyy" }`
- Blocked copy/paste fields: `reEmail`, `reTCKN`
- JS files: `/PageJs/Macaristan/TR/ankara/an-bir-{saatGetir,tarihGetir,turkiye}.js`, `an-makeAppointment.js`
- [x] ~~Fetch `an-makeAppointment.js` to extract field names~~ — Done, full source captured
- [ ] Implement auto-book for Ankara

### Istanbul (harder — do second)
Full source extracted to `Istanbul source files/`. Key findings:
- **Obfuscated field names** (from inline JS, can change per deploy):
  - TC ID:       `ast_cf41a9`
  - Name:        `asn_482313`
  - Surname:     `assn_104d25`
  - Email:       `ase_8e98ac`
  - Passport no: `aspassno_33af5d`
- reCAPTCHA v3 site key: `6Lf22HgrAAAAAP3u20U_HvrMsqmtltl7HcpezMWj`
- security-protection.js (DevTools detection, keyboard shortcut blocking, anti-debugger)
- FormNonce (anti-replay token)
- Double-submit protection (isSubmitting flag)
- 15-day travel date gap rule
- Submit endpoint: `POST /tr/istanbul-bireysel-basvuru`
- Date API: `POST /Macaristan/TarihGetir`
- Time API: `POST /Macaristan/SaatGetir`
- JS files: `/PageJs/Macaristan/TR/istanbul/{saatGetir,tarihGetir,turkiye,makeAppointment}.js`
- Blocked copy/paste fields: `reEmail`, `rEmail`, `rTCKN`
- [ ] Implement auto-book for Istanbul

### Existing Appointment Check (both cities)
`POST /tr/randevu-kontrol` — checks if user already has a booking. Blocks new booking if one exists.

### Key architectural insight
When no appointments are available, the server renders ONLY a static "no quota" message — **no form at all**. The form with dropdowns only appears when slots open. This means:
- Primary detection = checking if "no quota" text is in the HTML (what we do now)
- TarihGetir can only be called when form is visible (as secondary check for exact dates)
- tabId/countryid values are unknown until form renders

### Date checking API
- [x] ~~Try calling TarihGetir API directly for faster checks~~ — Implemented in hybrid bot. TarihGetir is called when slots are detected as secondary check.
- [x] ~~Load page once per session to get `__RequestVerificationToken`~~ — Implemented: Playwright extracts cookies + token, requests uses them for fast checks.

### Shared obstacles for auto-book
- [ ] **40-second bot trap** — `if (elapsedTime < 40)` rejects and redirects to google.com. Auto-book MUST wait 40+ seconds after page load
- [ ] Google reCAPTCHA **v3** (Istanbul only) — invisible scoring, action: `appointment_submit`
- [ ] Cloudflare Turnstile — token stored in `#cfToken` via `onTurnstileSuccess()` callback
- [ ] `formStartTime` — set to `Date.now()` on page load, checked server-side (40s minimum)
- [ ] `enteredCode` — manual verification code the user enters (unknown what generates it)
- [ ] Date picker constraint: appointment must be 15-45 days before travel date (unless "İş (Ticari)" business travel)

## Detection
- [x] ~~Upgrade to hybrid fast HTTP checks~~ — Done 2026-04-05. Playwright for session bootstrap, requests for fast checks (~200ms each). Tested: 58 checks/hour, zero errors.
- [ ] Add secondary form-presence check as fallback (in case phrase stays but form also appears)
- [ ] If page structure changes, detection phrase may silently break — add periodic verification

## Bot Detection / Stealth
- [ ] Rotate user-agent strings between checks
- [ ] Add `playwright-stealth` for Playwright session refreshes
- [ ] If blocked by reCAPTCHA: integrate 2captcha or CapSolver API (~$1-3/1000 solves)
- [ ] If still blocked: residential proxies (~$10-30/month) — last resort

## Defeating Cloudflare Turnstile (for auto-book)

Options, best to worst:

**Option 1 — Connect to user's real Chrome (best, free)**
- Playwright attaches to running Chrome via `connect_over_cdp("http://localhost:9222")`
- Turnstile sees real browser with real history/cookies/fingerprint — looks 100% human
- Launch Chrome: `chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\ChromeBot`
- Bot detects slot → connects to real Chrome → fills form → user clicks submit (or bot does)

**Option 2 — Humanized Playwright session (free, less reliable)**
- `playwright-stealth` + realistic mouse/typing + keeping page open 40+ seconds
- Hit or miss, Turnstile updates frequently

**Option 3 — CapSolver (paid, ~$1-3/1000)**
- Claims Turnstile support but inconsistent

Recommendation: Option 1 for auto-book. Detection stays as HTTP checks.

## Notifications
- [ ] Send a Telegram alert if the bot crashes or goes silent unexpectedly
