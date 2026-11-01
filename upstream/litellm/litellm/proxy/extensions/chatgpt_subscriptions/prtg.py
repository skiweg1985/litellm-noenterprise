"""Stable PRTG HTTP Data Advanced channel contract."""

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .service import MonitoringError

LOOKUP = "prtg.standardlookups.boolean.statetrueok"


def duration(seconds):
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size:g}{unit}"
    return f"{seconds:g}s"


def reset_time(timestamp):
    try:
        zone = ZoneInfo(os.getenv("CHATGPT_MONITORING_TIMEZONE", "UTC"))
        moment = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(zone)
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        raise MonitoringError("Invalid quota reset timestamp from OpenAI.") from None
    # Month names are fixed so the text does not depend on the process locale.
    month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[moment.month - 1]
    return f"{month} {moment.day} {moment:%H:%M} {moment.tzname()}"


def login_text(status):
    """Sensor message while the ChatGPT sign-in needs attention."""
    login = status.get("login")
    error = (status.get("error") or {}).get("message")
    if status["state"] == "login_pending" and login:
        prefix = "New sign-in pending" if status.get("token_valid") else "Login required"
        text = f"{prefix}. Continue in the subscription administration page at /chatgpt/ui"
    elif status["state"] == "logged_out":
        text = "Signed out. Start the sign-in on /chatgpt/ui"
    elif status["state"] == "token_expired":
        text = "Token expired, refresh pending"
    else:
        text = "Login required. Start the sign-in on /chatgpt/ui"
    return text + (f" | {error}" if error else "")


def render(snapshot, limit_id, windows):
    selected = windows.split(",")
    if selected not in (["primary"], ["secondary"], ["primary", "secondary"]):
        raise MonitoringError(
            "Invalid windows. Use primary, secondary or primary,secondary."
        )
    quota = next((g for g in snapshot["limits"] if g["id"] == limit_id), None)
    if quota is None or quota["allowed"] is None:
        raise MonitoringError("Requested quota group or availability is unavailable.")
    results = [
        {
            "channel": "Allowed",
            "value": int(quota["allowed"]),
            "unit": "Custom",
            "valuelookup": LOOKUP,
        }
    ]
    now = time.time()
    text = []
    for name in selected:
        w = quota[name]
        if w is None or any(
            w.get(key) is None
            for key in ("remaining_percent", "window_seconds", "reset_at")
        ):
            raise MonitoringError(
                "Requested quota window is unavailable. Check the windows selection."
            )
        label = name.capitalize()
        results.extend(
            [
                {
                    "channel": label + " Available",
                    "value": w["remaining_percent"],
                    "unit": "Percent",
                    "float": 1,
                },
                {
                    "channel": label + " Reset",
                    "value": max(0, int(w["reset_at"] - now)),
                    "unit": "TimeSeconds",
                },
            ]
        )
        text.append(
            f"{label} {duration(w['window_seconds'])}: "
            f"{w['remaining_percent']:g}% left, resets {reset_time(w['reset_at'])}"
        )
    results.append(
        {
            "channel": "Data Age",
            "value": max(0, int(now - snapshot["fetched_at"])),
            "unit": "TimeSeconds",
        }
    )
    return {"prtg": {"result": results, "text": " | ".join(text)}}
