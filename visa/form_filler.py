import asyncio
from datetime import datetime, timedelta

from visa.config import (
    ROTATING_FIELD_PLACEHOLDERS, STATIC_TEXT_FIELDS, STATIC_SELECT_FIELDS,
)
from visa.date_api import fetch_available_times

# Default valid window (days) between appointment date and travel date, used
# when the city's config entry doesn't specify. Per the site's own text:
#   Istanbul: minimum 15 calendar days between application and travel
#   Ankara:   minimum 17 calendar days
# The maximum (~60 days / 2 months) is an observed ceiling, not stated on-site.
DEFAULT_GAP_MIN = 17
DEFAULT_GAP_MAX = 60


def _compute_travel_date(picked_date, profile_travel_date, min_gap, max_gap):
    """
    Return the travel date to use. If the profile's date is within the valid
    window (min_gap–max_gap days after the appointment), use it. Otherwise
    compute a safe date a few days past the city minimum and return it along
    with a warning string; returns (date_str, warning_or_None).
    """
    if not picked_date:
        return profile_travel_date, None
    try:
        appt_dt = datetime.strptime(picked_date, "%Y-%m-%d")
    except Exception:
        return profile_travel_date, None

    if profile_travel_date:
        try:
            td_dt = datetime.strptime(profile_travel_date, "%Y-%m-%d")
            gap = (td_dt - appt_dt).days
            if min_gap <= gap <= max_gap:
                return profile_travel_date, None
            warning = (
                f"Profile travel_date {profile_travel_date} is {gap} days after "
                f"appointment {picked_date} (valid: {min_gap}–{max_gap}). "
                f"Using computed date instead."
            )
        except Exception:
            warning = f"Could not parse profile travel_date '{profile_travel_date}'; using computed."
    else:
        warning = "No travel_date in profile; using computed date."

    # Sit a few days above the city minimum, but never past the max ceiling.
    offset = min(min_gap + 3, max_gap)
    safe = (appt_dt + timedelta(days=offset)).strftime("%Y-%m-%d")
    return safe, warning


async def fill_form(page, profile, entry, http_session, picked_date):
    """
    Fill all personal-info fields from the booking profile.
    Honeypot fields (hp_*) are never touched — only mapped fields are filled.
    Returns (filled, failed, times).

    Does NOT fill enteredCode — that must be filled in the booker right before
    submit because the code expires within ~60 seconds of the page rendering it.

    Fill order matters: selects (esp. TravelSubject) must come first because their
    change events reset downstream DOM. Personal info is filled last so it survives.
    """
    filled, failed = [], []

    # ── 1. Static selects ────────────────────────────────────────────────────
    for sel, key in STATIC_SELECT_FIELDS.items():
        value = profile.get(key, "")
        if not value:
            continue
        try:
            await page.select_option(sel, value)
            filled.append(key)
        except Exception as e:
            failed.append(f"{key}: {e}")

    await asyncio.sleep(0.5)

    # ── 2. TravelDate ────────────────────────────────────────────────────────
    # The TravelDate datepicker is filtered: only dates 17–60 days before the
    # travel date that also have open appointment slots are shown. Using a
    # profile date outside that window silently no-ops and leaves the field
    # empty, which then cascades to reset AppointmentDate, AppointmentTime,
    # and all personal info via the page's JS.
    min_gap = int(entry.get("min_gap_days", DEFAULT_GAP_MIN))
    max_gap = int(entry.get("max_gap_days", DEFAULT_GAP_MAX))
    travel_date_val, td_warning = _compute_travel_date(
        picked_date, profile.get("travel_date", ""), min_gap, max_gap
    )
    if td_warning:
        failed.append(f"travel_date warning: {td_warning}")

    if travel_date_val:
        try:
            y, mo, d = str(travel_date_val).split("-")
            td_actual = await page.evaluate(
                "(args) => {"
                "  const date = new Date(args.y, args.m - 1, args.d);"
                "  const $td = $('#TravelDate');"
                "  if (!$td.length) return null;"
                "  $td.datepicker('setDate', date);"
                "  const got = $td.datepicker('getDate');"
                "  return (got instanceof Date && !isNaN(got)) ? got.toISOString().slice(0,10) : null;"
                "}",
                {"y": int(y), "m": int(mo), "d": int(d)},
            )
            if td_actual:
                filled.append(f"travel_date ({td_actual})")
            else:
                failed.append(
                    f"travel_date: datepicker rejected {travel_date_val} — "
                    "date may still be outside the filtered window"
                )
        except Exception as e:
            failed.append(f"travel_date: {e}")

    # ── 3. Appointment datepicker + time slots ────────────────────────────────
    times = []
    if picked_date:
        try:
            await page.wait_for_selector("#apDate", state="visible", timeout=5_000)
        except Exception:
            failed.append("datepicker: #apDate never appeared — TravelDate may not have fired correctly")

        try:
            y, mo, d = picked_date.split("-")
            dp_actual = await page.evaluate(
                "(args) => {"
                "  const date = new Date(args.y, args.m - 1, args.d);"
                "  const $dp = $('#datepicker');"
                "  if (!$dp.length) return null;"
                "  $dp.datepicker('setDate', date);"
                "  const got = $dp.datepicker('getDate');"
                "  return (got instanceof Date && !isNaN(got)) ? got.toISOString().slice(0,10) : null;"
                "}",
                {"y": int(y), "m": int(mo), "d": int(d)},
            )
            if dp_actual:
                filled.append(f"datepicker ({dp_actual})")
            else:
                failed.append("datepicker: widget returned null/invalid after setDate — date outside allowed range or #apDate not ready")
        except Exception as e:
            failed.append(f"datepicker: {e}")

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

    # ── 4. Personal info ──────────────────────────────────────────────────────

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

    # Email primary: new form uses name="Email" (static), old form uses ase_<hex> (rotating).
    # Try static name first, then fall back to rotating prefix.
    try:
        em = profile.get("email", "")
        if em:
            result = await page.evaluate(
                "(v) => {"
                "  const byStatic = document.querySelector('input[name=\"Email\"]');"
                "  if (byStatic) {"
                "    byStatic.value = v;"
                "    byStatic.dispatchEvent(new Event('input',{bubbles:true}));"
                "    byStatic.dispatchEvent(new Event('change',{bubbles:true}));"
                "    return 'static';"
                "  }"
                "  const inputs = document.querySelectorAll('input[placeholder=\"E-posta Giriniz\"]');"
                "  for (const i of inputs) {"
                "    if (i.name && i.name.startsWith('ase_')) {"
                "      i.value = v;"
                "      i.dispatchEvent(new Event('input',{bubbles:true}));"
                "      i.dispatchEvent(new Event('change',{bubbles:true}));"
                "      return 'rotating';"
                "    }"
                "  }"
                "  return null;"
                "}",
                em,
            )
            if result:
                filled.append(f"email ({result})")
            else:
                failed.append("email: no Email or ase_ field found in DOM")
    except Exception as e:
        failed.append(f"email: {e}")

    # Email confirm — Istanbul uses rEmail, Ankara uses reEmail
    try:
        em = profile.get("email_confirm", "") or profile.get("email", "")
        if em:
            sel = None
            for candidate in ('input[name="reEmail"]', 'input[name="rEmail"]'):
                if await page.query_selector(candidate):
                    sel = candidate
                    break
            if sel:
                await page.fill(sel, em)
                filled.append("email_confirm")
            else:
                failed.append("email_confirm: neither reEmail nor rEmail found in DOM")
    except Exception as e:
        failed.append(f"email_confirm: {e}")

    # passport_expiry — field is gone from the new Ankara form but may be
    # dynamically injected by the wizard JS. Try jQuery datepicker API first,
    # fall back to direct value injection, and treat absence as a soft warning.
    passport_expiry_val = profile.get("passport_expiry", "")
    if passport_expiry_val:
        try:
            y, m, d = str(passport_expiry_val).split("-")
            result = await page.evaluate(
                "(args) => {"
                "  const $el = $('#passportEndDate');"
                "  if ($el.length && $el.data('datepicker')) {"
                "    const date = new Date(args.y, args.m - 1, args.d);"
                "    $el.datepicker('setDate', date);"
                "    const got = $el.datepicker('getDate');"
                "    return (got instanceof Date && !isNaN(got)) ? got.toISOString().slice(0,10) : 'set-no-readback';"
                "  }"
                "  const el = document.getElementById('passportEndDate');"
                "  if (el) {"
                "    el.value = args.v;"
                "    el.dispatchEvent(new Event('input',{bubbles:true}));"
                "    el.dispatchEvent(new Event('change',{bubbles:true}));"
                "    return 'fallback-injected';"
                "  }"
                "  return null;"
                "}",
                {"y": int(y), "m": int(m), "d": int(d),
                 "v": f"{int(d):02d}/{int(m):02d}/{y}"},
            )
            if result:
                filled.append(f"passport_expiry ({result})")
            else:
                failed.append(
                    "passport_expiry: #passportEndDate not in DOM — "
                    "field may appear after TravelDate is set (wizard step) — check DOM capture"
                )
        except Exception as e:
            failed.append(f"passport_expiry: {e}")

    # Static text fields (non-date, non-rotating, non-email)
    for sel, key in STATIC_TEXT_FIELDS.items():
        if key == "passport_expiry":
            continue  # handled above
        value = profile.get(key, "")
        if not value:
            continue
        try:
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

    return filled, failed, times
