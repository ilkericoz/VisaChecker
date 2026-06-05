import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

HEARTBEAT_INTERVAL = 6 * 60 * 60
SESSION_REFRESH_INTERVAL = 15 * 60
IP_BAN_AUTO_RESUME_AFTER = 15 * 60
SNAPSHOTS_DIR = Path("snapshots")
SANITY_PHRASE = "Randevu"
FORM_MARKER = 'id="AppointmentTabID"'
CSRF_MARKER = '__RequestVerificationToken'
TARIH_GETIR_BASE = "https://appointment.as-visa.com"
BOOKING_PROFILE_PATH = "booking_profile.json"
CHROME_CDP_URL = "http://localhost:9222"
CHROME_PROFILE_DIR = Path("chrome_visa_profile").resolve()

# Form fields whose `name` rotates every page render (anti-scraper).
# Map by the placeholder text (which IS stable) → profile key.
ROTATING_FIELD_PLACEHOLDERS = {
    "Adınızı Giriniz":         "first_name",
    "Soyadınızı Giriniz":      "last_name",
    "Pasaport No Giriniz":     "passport_no",
    "T.C. Kimlik No Giriniz":  "tc_kimlik",
}
# Static (non-rotating) text inputs: selector → profile key
STATIC_TEXT_FIELDS = {
    'input[name="reTCKN"]':        "tc_kimlik_confirm",
    'input[name="DogumYili"]':     "birth_year",
    'input[name="Phone"]':         "phone",
    # passport_expiry is a readonly datepicker — injected via JS as dd/mm/yyyy
    'input#passportEndDate':       "passport_expiry",
    # TravelDate handled separately via jQuery datepicker API
}
# Static <select> fields: selector → profile key
STATIC_SELECT_FIELDS = {
    'select#NationalityTabID': "nationality",
    'select#AppointmentTabID': "appointment_type",
    'select#TravelSubject':    "travel_purpose",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class IPBannedError(Exception):
    pass


def load_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def load_booking_profile():
    try:
        with open(BOOKING_PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[!] Booking profile load failed: {e}")
        return None
