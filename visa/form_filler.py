import asyncio

from visa.config import (
    ROTATING_FIELD_PLACEHOLDERS, STATIC_TEXT_FIELDS, STATIC_SELECT_FIELDS,
)
from visa.captcha import solve_entered_code
from visa.date_api import fetch_available_times


async def fill_form(page, profile, entry, http_session, picked_date):
    """
    Fill all personal-info fields from the booking profile.
    Honeypot fields (hp_*) are never touched — only mapped fields are filled.
    Returns (filled, failed, times) where times is [{value, text}, ...] for the time select.
    """
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

    # Email primary: rotating name (ase_<hex>) — identified by placeholder
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

    # Static text fields (handle readonly date inputs via JS)
    for sel, key in STATIC_TEXT_FIELDS.items():
        value = profile.get(key, "")
        if not value:
            continue
        try:
            # passport_expiry is a readonly datepicker — inject dd/mm/yyyy via JS
            if key == "passport_expiry":
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

    # TravelDate: must use jQuery datepicker API (not el.value) to fire
    # the changeDate event, which shows #apDate and sets the valid date
    # range on the appointment datepicker. Must run AFTER TravelSubject is set.
    travel_date_val = profile.get("travel_date", "")
    if travel_date_val:
        try:
            y, mo, d = str(travel_date_val).split("-")
            await page.evaluate(
                "(args) => {"
                "  const date = new Date(args.y, args.m - 1, args.d);"
                "  const $td = $('#TravelDate');"
                "  if ($td.length) {"
                "    $td.datepicker('setDate', date);"
                "  }"
                "}",
                {"y": int(y), "m": int(mo), "d": int(d)},
            )
            filled.append("travel_date")
        except Exception as e:
            failed.append(f"travel_date: {e}")

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

    # enteredCode — OCR the CAPTCHA PNG inside label[for="enteredCode"] img
    try:
        entered_code, err = await solve_entered_code(page)
        if entered_code:
            await page.fill('input[name="enteredCode"]', entered_code)
            filled.append(f"enteredCode ({entered_code})")
        else:
            failed.append(f"enteredCode: {err}")
    except Exception as e:
        failed.append(f"enteredCode: {e}")

    return filled, failed, times
