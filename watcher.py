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
        "end": (12, 40),
    },
    {
        "name": "donhanna710",
        "url": "https://pollev.com/donhanna710",
        "days": {0, 2},  # Mon, Wed
        "start": (8, 0),
        "end": (9, 40),
    },
]

POLL_INTERVAL_ACTIVE = 15  # seconds between checks while inside a scheduled window
NAV_TIMEOUT_MS = 20000
STATUS_WAIT_TIMEOUT_S = 15  # how long to wait for the real status text to settle after load
CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 8  # ~2 min of back-to-back failures looks like a real break, not a blip

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
            headers={"Title": title, "Priority": "urgent", "Tags": "warning"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.URLError as e:
        log(f"ntfy notification failed: {e}")


def notify_failure(reason):
    """Last-resort alert for failures that would otherwise go completely unnoticed:
    repeated check_site() failures (site markup/WAF changed) or any unexpected
    crash. A failure to even send this alert still propagates (the workflow's
    own failure/job-level alert step is the final backstop)."""
    log(f"ALERTING ON FAILURE: {reason}")
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=reason[:300].encode("utf-8"),
            headers={"Title": "PollEv watcher BROKEN", "Priority": "urgent", "Tags": "rotating_light"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.URLError as e:
        log(f"failure-alert itself failed to send: {e}")


def main():
    log("Watcher started")

    if os.environ.get("WATCHER_TEST_NOTIFY") == "1":
        log("WATCHER_TEST_NOTIFY=1: sending a test notification, then exiting.")
        notify({"name": "test"}, "This is a test notification from watcher.py.")
        return 0

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
    consecutive_failures = {site["name"]: 0 for site in SITES}
    failure_alerted = {site["name"]: False for site in SITES}
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
                        consecutive_failures[site["name"]] += 1
                        if (
                            consecutive_failures[site["name"]] >= CONSECUTIVE_FAILURE_ALERT_THRESHOLD
                            and not failure_alerted[site["name"]]
                        ):
                            notify_failure(
                                f"{site['name']}: {consecutive_failures[site['name']]} checks in a row failed "
                                f"({e}). The page may have changed, or something's blocking it - "
                                f"it may not be able to notify you even if the poll opens."
                            )
                            failure_alerted[site["name"]] = True
                        continue
                    consecutive_failures[site["name"]] = 0

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
    try:
        exit_code = main()
    except Exception as e:
        # Anything that escapes main() (browser launch failure, etc.) would
        # otherwise just fail the GitHub Actions job with no alert to the
        # phone. The workflow's own failure-handler step is the final
        # backstop if even this fails to send.
        notify_failure(f"watcher crashed: {e}")
        raise
    sys.exit(exit_code)
