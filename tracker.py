#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import certifi
except ImportError:
    certifi = None

BASE = "https://www.eventcinemas.com.au"
CINEMA_ID = 96
CINEMA_NAME = "IMAX Sydney"
RELEASE_DATE = date(2026, 12, 16)

STATE_FILE = Path(__file__).with_name("state.json")
CONFIG_FILE = Path(__file__).with_name("config.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

SSL_CONTEXT = (
    ssl.create_default_context(cafile=certifi.where())
    if certifi
    else ssl.create_default_context()
)

DEFAULT_CONFIG = {
    "movie": "Dune: Part Three",
    "days_ahead": 90,
    "rows": "L,M,P",
    "middle_only": True,
    "middle_width": 14,
    "discord_webhook": "",
    "check_seats": True,
}

OPEN_STATUSES = {
    "available",
    "open",
    "free",
    "available for sale",
    "available for booking",
}


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def load_json_file(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return fallback


def save_json_file(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25, context=SSL_CONTEXT) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8-sig"))


def get_sessions_for_date(day: str, movie_query: str) -> tuple[list[dict], list[dict]]:
    url = f"{BASE}/Cinemas/GetSessions?cinemaIds={CINEMA_ID}&date={day}"
    data = get_json(url)
    matches: list[dict] = []
    movie_meta: list[dict] = []
    wanted = norm(movie_query)

    for movie in data.get("Data", {}).get("Movies", []) or []:
        title = movie.get("Title") or movie.get("Name") or ""
        movie_id = movie.get("Id")
        ntitle = norm(title)
        if wanted and (ntitle == wanted or wanted in ntitle or ntitle in wanted):
            movie_meta.append({"id": movie_id, "title": title})
            for cinema_model in movie.get("CinemaModels", []) or []:
                matches.extend(cinema_model.get("Sessions", []) or [])
    return matches, movie_meta


def get_seat_map(session_id: int) -> dict:
    url = f"{BASE}/Ticketing/Order/GetSeating?sessionId={session_id}"
    return get_json(url)


def session_seats_available(session: dict) -> int | None:
    value = session.get("SeatsAvailable")
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        pass
    return None


def parse_start_time(raw: Any) -> tuple[str, str]:
    if not raw:
        return "Unknown date", "Unknown time"
    s = str(raw)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%a %d %b %Y"), dt.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})", s)
    if m:
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        h = int(m.group(2))
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return d.strftime("%a %d %b %Y"), f"{h12}:{m.group(3)} {ampm}"
    return s, ""


def session_booking_url(session: dict) -> str:
    for key in ("BookingUrl", "BookingURL", "BookingLink", "BookUrl", "BookURL", "Url"):
        value = session.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def seat_rows_from_response(seat_data: dict) -> list[dict]:
    data = seat_data.get("Data") or {}
    seats_obj = data.get("Seats") or {}
    rows = seats_obj.get("Rows")
    if isinstance(rows, list):
        return rows

    found: list[dict] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, dict):
            candidate = obj.get("Rows")
            if isinstance(candidate, list) and candidate and all(isinstance(x, dict) for x in candidate):
                found.extend(candidate)
                return
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj[:50]:
                walk(v, depth + 1)

    walk(seat_data)
    return found


def parse_seat_map(seat_data: dict, target_rows: set[str], middle_only: bool, middle_width: int) -> dict:
    rows = seat_rows_from_response(seat_data)
    target_upper = {r.upper() for r in target_rows}
    result = {
        "target_seats": [],
        "target_middle_seats": [],
    }

    if not rows:
        return result

    for row in rows:
        row_name = str(row.get("RowName") or row.get("Name") or row.get("Row") or "").strip()
        if not row_name:
            continue

        seats = row.get("Seats") or row.get("seats") or []
        if not isinstance(seats, list):
            continue

        valid_seats_in_row = []
        for index, seat in enumerate(seats):
            if not isinstance(seat, dict):
                continue
            raw_seat_name = seat.get("SeatName") or seat.get("seatName") or seat.get("DisplayName") or seat.get("SeatNumber")
            if raw_seat_name is None:
                continue
            seat_name_str = str(raw_seat_name).strip()
            if not seat_name_str or seat_name_str.lower() in {"spacer", "aisle", "none", "null"}:
                continue

            seat_label = seat_name_str.upper() if seat_name_str.upper().startswith(row_name.upper()) else f"{row_name.upper()}{seat_name_str.upper()}"
            status = str(seat.get("Status") or "").strip().lower()

            valid_seats_in_row.append({
                "index": index,
                "label": seat_label,
                "open": status in OPEN_STATUSES,
            })

        seen_labels = set()
        deduped_records = []
        for r in valid_seats_in_row:
            if r["label"] not in seen_labels:
                seen_labels.add(r["label"])
                deduped_records.append(r)

        if row_name.upper() not in target_upper:
            continue

        open_records = [r for r in deduped_records if r["open"]]
        for r in open_records:
            result["target_seats"].append(r["label"])

        if middle_only and deduped_records:
            width = max(1, min(middle_width, len(deduped_records)))
            start = max(0, (len(deduped_records) - width) // 2)
            middle_records = deduped_records[start : start + width]
            for r in middle_records:
                if r["open"]:
                    result["target_middle_seats"].append(r["label"])

    return result


def send_discord(webhook: str, title: str, body: str, url: str = "") -> None:
    if not webhook:
        return
    content = f"**{title}**\n{body}"
    if url:
        content += f"\n[Book Tickets Here]({url})"
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT):
        pass


def run_tracker():
    # Merge loaded config onto defaults to prevent KeyError
    cfg = DEFAULT_CONFIG.copy()
    user_cfg = load_json_file(CONFIG_FILE, {})
    if isinstance(user_cfg, dict):
        cfg.update(user_cfg)

    # Webhook comes from the DISCORD_WEBHOOK env var (set via a GitHub secret in CI).
    # Falls back to a "discord_webhook" key in config.json only if present, for local testing.
    webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("discord_webhook", "")
    if not webhook:
        print("WARNING: No Discord webhook configured (set DISCORD_WEBHOOK env var). Notifications will be skipped.")

    movie = cfg.get("movie", "Dune: Part Three")
    target_rows = {x.strip().upper() for x in str(cfg.get("rows", "L,M,P")).split(",") if x.strip()}

    state = load_json_file(
        STATE_FILE,
        {
            "known_session_ids": [],
            "target_counts": {},
            "last_total_counts": {},
            "bootstrapped_movies": [],
        },
    )

    # Dynamic date range: scan starting today to capture advance screenings
    start_date = date.today()
    post_release_end = RELEASE_DATE + timedelta(days=30)
    
    # Calculate how many days needed to span from today through post-release window
    calculated_days = (post_release_end - start_date).days
    days_ahead = max(int(cfg.get("days_ahead", 90)), calculated_days)

    print(f"Scanning '{movie}' from {start_date.isoformat()} for {days_ahead} days...")

    all_sessions: list[dict] = []
    pending_notifications: list[dict] = []

    for i in range(days_ahead):
        d = start_date + timedelta(days=i)
        day = d.isoformat()
        try:
            sessions, _ = get_sessions_for_date(day, movie)
            all_sessions.extend(sessions)
        except urllib.error.HTTPError as e:
            print(f"Date {day}: HTTP {e.code}")
        except Exception as e:
            print(f"Date {day}: Error {e}")

    dedup: dict[str, dict] = {str(s["Id"]): s for s in all_sessions if s.get("Id") is not None}
    sessions = list(dedup.values())
    print(f"Found {len(sessions)} matching sessions.")

    known_ids = {str(x) for x in state.get("known_session_ids", [])}
    target_counts = dict(state.get("target_counts", {}))
    total_counts = dict(state.get("last_total_counts", {}))
    boot_key = norm(movie)
    bootstrapped = boot_key in set(state.get("bootstrapped_movies", []))

    for s in sorted(sessions, key=lambda x: str(x.get("StartTime", ""))):
        sid = str(s["Id"])
        start_raw = s.get("StartTime")
        day_text, time_text = parse_start_time(start_raw)
        overall = session_seats_available(s)
        book_url = session_booking_url(s)

        if not bootstrapped and sid not in known_ids:
            known_ids.add(sid)
        elif bootstrapped and sid not in known_ids:
            known_ids.add(sid)
            body = (
                f"**Movie:** {movie}\n"
                f"**Cinema:** {CINEMA_NAME}\n"
                f"**Date:** {day_text}\n"
                f"**Time:** {time_text}\n"
                f"**Seats Available:** {overall if overall is not None else 'Unknown'}"
            )
            print(f"NEW SESSION DETECTED: {day_text} {time_text}")
            pending_notifications.append({
                "title": f"🚨 NEW SESSION ADDED — {CINEMA_NAME}",
                "body": body,
                "book_url": book_url,
            })

        if cfg.get("check_seats", True) and overall is not None and overall > 0 and target_rows:
            try:
                seat_data = get_seat_map(int(sid))
                parsed = parse_seat_map(
                    seat_data,
                    target_rows,
                    bool(cfg.get("middle_only", True)),
                    int(cfg.get("middle_width", 14)),
                )

                active_seats = parsed["target_middle_seats"] if cfg.get("middle_only", True) else parsed["target_seats"]
                current_target = len(active_seats)
                prev_target = target_counts.get(sid) or 0

                if bootstrapped and current_target > prev_target:
                    seat_str = ", ".join(active_seats)
                    body = (
                        f"**Movie:** {movie}\n"
                        f"**Cinema:** {CINEMA_NAME}\n"
                        f"**Date:** {day_text} at {time_text}\n"
                        f"**Available Seats ({cfg.get('rows', 'L,M,P')}):** {seat_str}"
                    )
                    print(f"NEW CENTER SEATS OPEN: {day_text} {time_text} -> [{seat_str}]")
                    pending_notifications.append({
                        "title": f"🎟 CENTER SEATS AVAILABLE — {CINEMA_NAME}",
                        "body": body,
                        "book_url": book_url,
                    })

                target_counts[sid] = current_target
            except Exception as e:
                print(f"Seat check failed for session {sid}: {e}")

        if overall is not None:
            total_counts[sid] = overall

    if pending_notifications:
        print(f"Sending {len(pending_notifications)} Discord notification(s)...")
        for n in pending_notifications:
            try:
                send_discord(webhook, n["title"], n["body"], n["book_url"])
            except Exception as e:
                print(f"Discord alert error: {e}")

    state["known_session_ids"] = sorted(known_ids)
    state["target_counts"] = target_counts
    state["last_total_counts"] = total_counts
    movies = set(state.get("bootstrapped_movies", []))
    movies.add(boot_key)
    state["bootstrapped_movies"] = sorted(movies)
    save_json_file(STATE_FILE, state)

    print("Execution complete.")


if __name__ == "__main__":
    run_tracker()
