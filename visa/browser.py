import os
import socket
import subprocess
import time

from visa.config import CHROME_PROFILE_DIR

DEFAULT_CDP_PORT = 9222


def _find_chrome() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def cdp_port(config) -> int:
    """The CDP port this bot uses. Must be unique per bot to avoid two bots
    sharing one Chrome instance (which corrupts sessions / mixes profiles)."""
    try:
        return int(config.get("cdp_port", DEFAULT_CDP_PORT))
    except Exception:
        return DEFAULT_CDP_PORT


def cdp_url(config) -> str:
    return f"http://localhost:{cdp_port(config)}"


def _is_cdp_up(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def launch_chrome_cdp(config) -> bool:
    """
    Launch Chrome with remote debugging on the configured port using the
    dedicated visa profile. Returns True once the CDP port is ready (up to 15s).
    If Chrome is already listening on the port, returns True immediately.

    IMPORTANT: the port + the user-data-dir together must be unique to THIS bot.
    If another bot (e.g. FareHarbor) uses the same port, this bot will silently
    attach to that bot's Chrome — wrong profile, no warm Cloudflare cookies, and
    two Playwright clients fighting over the same pages. Each bot needs its own
    cdp_port AND its own profile dir.
    """
    port = cdp_port(config)
    if _is_cdp_up(port):
        return True

    chrome = config.get("chrome_path") or _find_chrome()
    if not chrome:
        print("[Chrome] Could not find chrome.exe — add 'chrome_path' to config.json")
        return False

    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            [
                chrome,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={CHROME_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                # Removes the AutomationControlled blink feature, which is what
                # sets navigator.webdriver=true under remote debugging. With this
                # flag webdriver reports the *native* false — no JS patching needed
                # (and JS patching to `undefined` is itself a tell: real Chrome = false).
                "--disable-blink-features=AutomationControlled",
                # Suppress the "Chrome is being controlled by automated test
                # software" infobar/automation switches.
                "--excludeSwitches=enable-automation",
                "--disable-infobars",
            ],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        print(f"[Chrome] Launch failed: {e}")
        return False

    for _ in range(30):          # wait up to 15 s
        time.sleep(0.5)
        if _is_cdp_up(port):
            return True
    print("[Chrome] Timed out waiting for CDP port")
    return False
