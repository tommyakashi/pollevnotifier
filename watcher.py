#!/usr/bin/env python3
"""Watches PollEv pages on a weekly schedule and notifies on change from the default screen."""

import os
import re
import sys
import time
import urllib.request
import urllib.error
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "pollev-f2e7f798f19b0cb4")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

TZ = ZoneInfo("America/Los_Angeles")

# weekday(): Monday=0 ... Sunday=6
SITES = [
    {
        "name": "religion",
        "url": "https://pollev.com/religion",
        "days": {1, 3},  # Tue, Thu
        "start": (11, 0),
        "end": (12, 30),
    },
    {
        "name": "donhanna710",
        "url": "https://pollev.com/donhanna710",
        "days": {0, 2},  # Mon, Wed
        "start": (8, 0),
        "end": (9, 30),
    },
]

POLL_INTERVAL_ACTIVE = 15  # seconds between checks while inside a scheduled window
NAV_TIMEOUT_MS = 20000
STATUS_WAIT_TIMEOUT_S = 15  # how long to wait for the real status text to settle after load

WAITING_PATTERN = re.compile(r"waiting for.*presentation to begin", re.IGNORECASE)

# Grabs the page's status heading while ignoring the "introduce yourself" modal,
# which also renders an <h1> briefly inside <main> before React portals it out.
STATUS_H1_JS = """
() => {
    const h1s = Array.from(document.querySelectorAll('h1'));
    const target = h1s.find(h => !h.closest('[role="dialog"]'));
    return target ? target.textContent.trim() : null;
}
"""


def log(msg):
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{ts}] {msg}", flush=True)


def in_window(site, now):
    if now.weekday() not in site["days"]:
        return False
    start = now.replace(hour=site["start"][0], minute=site["start"][1], second=0, microsecond=0)
    end = now.replace(hour=site["end"][0], minute=site["end"][1], second=0, microsecond=0)
    return start <= now <= end


def check_site(page, site):
    page.goto(site["url"], wait_until="load", timeout=NAV_TIMEOUT_MS)
    deadline = time.monotonic() + STATUS_WAIT_TIMEOUT_S
    text = None
    while time.monotonic() < deadline:
        try:
            text = page.evaluate(STATUS_H1_JS)
        except Exception:
            text = None  # page still navigating client-side; retry
        if text:
            return text
        time.sleep(0.3)
    raise RuntimeError(f"status heading never appeared for {site['name']}")


def notify(site, text):
    title = f"PollEv changed: {site['name']}"
    message = text[:200]

    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                check=False,
                timeout=10,
            )
        except Exception as e:
            log(f"desktop notification failed: {e}")

    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.URLError as e:
        log(f"ntfy notification failed: {e}")


def main():
    log("Watcher started")

    selftest = os.environ.get("WATCHER_SELFTEST") == "1"

    now = datetime.now(TZ)
    active_sites = [s for s in SITES if in_window(s, now)] if not selftest else list(SITES)
    if not active_sites:
        log("Started outside any scheduled window; nothing to do, exiting.")
        return 0

    if selftest:
        log("WATCHER_SELFTEST=1: checking all sites once regardless of schedule, then exiting.")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                for site in active_sites:
                    try:
                        text = check_site(page, site)
                        log(f"{site['name']}: {text!r}")
                    except Exception as e:
                        log(f"{site['name']}: check failed ({e})")
            finally:
                browser.close()
        return 0

    notified = {site["name"]: False for site in SITES}
    names = ", ".join(s["name"] for s in active_sites)
    log(f"Entering scheduled window for: {names} (checking every {POLL_INTERVAL_ACTIVE}s)")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            while True:
                now = datetime.now(TZ)
                active_sites = [s for s in SITES if in_window(s, now)]
                if not active_sites:
                    log("Scheduled window(s) ended; exiting.")
                    break

                for site in active_sites:
                    try:
                        text = check_site(page, site)
                    except Exception as e:
                        log(f"{site['name']}: check failed ({e})")
                        continue

                    if WAITING_PATTERN.search(text):
                        if notified[site["name"]]:
                            log(f"{site['name']}: back to waiting screen")
                        else:
                            log(f"{site['name']}: checked, still waiting (default)")
                        notified[site["name"]] = False
                    else:
                        if not notified[site["name"]]:
                            log(f"{site['name']}: CHANGED -> {text!r}")
                            notify(site, text)
                            notified[site["name"]] = True
                        else:
                            log(f"{site['name']}: still changed (already notified)")

                time.sleep(POLL_INTERVAL_ACTIVE)
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
