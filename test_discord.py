#!/usr/bin/env python3
"""
Standalone Discord webhook test.
Sends one test message using the same DISCORD_WEBHOOK secret the real
tracker uses, so you can confirm the secret + webhook plumbing works
independently of the seat-tracking logic.
"""
import json
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    import certifi
except ImportError:
    certifi = None

SSL_CONTEXT = (
    ssl.create_default_context(cafile=certifi.where())
    if certifi
    else ssl.create_default_context()
)

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (IMAX-Tracker-Test)",
}


def main() -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK env var is not set. Check the repo secret.")
        raise SystemExit(1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    content = (
        f"**✅ Test message from IMAX Seat Tracker**\n"
        f"If you're seeing this, your `DISCORD_WEBHOOK` secret and webhook "
        f"are wired up correctly.\nSent at: {now}"
    )

    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload, headers=HEADERS, method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            print(f"Discord responded with status {resp.status}. Check your channel.")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Discord returned an error: HTTP {e.code}\n{body}")
        raise SystemExit(1)
    except Exception as e:
        print(f"Failed to reach Discord: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
