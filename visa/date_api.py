from visa.config import TARIH_GETIR_BASE


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
