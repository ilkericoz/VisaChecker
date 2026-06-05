import os
import socket
import subprocess
import time

from visa.config import CHROME_PROFILE_DIR


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


def _is_cdp_up() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 9222), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def launch_chrome_cdp(config) -> bool:
    """
    Launch Chrome with remote debugging on port 9222 using the dedicated visa
    profile. Returns True once the CDP port is ready (up to 15s).
    If Chrome is already listening on 9222, returns True immediately.
    """
    if _is_cdp_up():
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
                "--remote-debugging-port=9222",
                f"--user-data-dir={CHROME_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        print(f"[Chrome] Launch failed: {e}")
        return False

    for _ in range(30):          # wait up to 15 s
        time.sleep(0.5)
        if _is_cdp_up():
            return True
    print("[Chrome] Timed out waiting for CDP port")
    return False
