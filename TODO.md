# TODO / Future Improvements

## Bot Behavior
- [ ] `/screenshot` should also run availability check and alert if slot found (currently separate from detection logic)
- [ ] Auto-book when slot is found (need to analyze saved HTML for form structure)
- [ ] Re-alert every X minutes if slot is still open (in case first notification is missed)

## Detection
- [ ] Add secondary form-presence check as fallback (in case phrase stays in DOM but form also appears) — need real HTML from an open slot to find correct field selectors first
- [ ] If page structure changes, detection phrase may silently break — add a way to verify detection is still working (e.g. monthly test alert)

## Bot Detection / Stealth
- [ ] Add random mouse movement / scroll before checking
- [ ] Rotate user-agent strings between checks
- [ ] Add `playwright-stealth` (free, patches more fingerprint signals beyond navigator.webdriver) — `pip install playwright-stealth`, then `await stealth_async(page)` after page creation
- [ ] If actually blocked by reCAPTCHA: integrate 2captcha or CapSolver API (~$1-3/1000 solves) to auto-solve and inject token
- [ ] If still blocked: residential proxies (~$10-30/month) — last resort

## Notifications
- [ ] Send a Telegram alert if the bot crashes or goes silent unexpectedly
