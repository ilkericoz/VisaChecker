# VisaChecker

Monitors the [AS-VISA](https://appointment.as-visa.com) appointment portal for Hungary Schengen visa slots in Istanbul and Ankara, and alerts you the moment one opens up.

## How it works

The site shows a "no quota" warning when there are no available slots. The bot checks both city pages every 3–10 minutes (randomized) and notifies you when that warning disappears.

When a slot is detected:
- Windows desktop notification
- Audible alert
- Screenshot saved (`found_<city>_<timestamp>.png`)
- Full page HTML saved (`found_<city>_<timestamp>.html`) for implementing auto-booking later

## Setup

**Requirements:** Python 3.10+

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python bot.py
```

## Configuration

Edit `config.json` to adjust settings:

| Key | Default | Description |
|-----|---------|-------------|
| `check_interval_min_seconds` | 180 | Minimum wait between checks (3 min) |
| `check_interval_max_seconds` | 600 | Maximum wait between checks (10 min) |
| `headless` | true | Run browser in background |
| `screenshot_on_found` | true | Save screenshot + HTML when slot found |
