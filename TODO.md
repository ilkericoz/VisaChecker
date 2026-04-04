# TODO / Future Improvements

## Bot Behavior
- [ ] `/screenshot` should also run availability check and alert if slot found (currently separate from detection logic)
- [ ] Auto-book when slot is found (need to analyze saved HTML for form structure)
- [ ] Re-alert every X minutes if slot is still open (in case first notification is missed)

## Detection
- [ ] If page structure changes, detection phrase may silently break — add a way to verify detection is still working (e.g. monthly test alert)

## Bot Detection / Stealth
- [ ] Add random mouse movement / scroll before checking
- [ ] Rotate user-agent strings between checks

## Notifications
- [ ] Send a Telegram alert if the bot crashes or goes silent unexpectedly
