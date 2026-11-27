"""Quota validity and weighted shares. All percentages are in percentage points."""
import math
from datetime import datetime, timezone

WINDOWS = ("primary", "secondary")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def quota_state(snapshot, now, interval):
    if not isinstance(snapshot, dict) or not _number(snapshot.get("fetched_at")):
        return "unavailable"
    if snapshot["fetched_at"] > now:
        return "unavailable"
    for name in WINDOWS:
        window = snapshot.get(name)
        if not isinstance(window, dict):
            return "unavailable"
        used, duration, reset = (window.get(k) for k in ("used_percent", "window_seconds", "reset_at"))
        if not all(_number(x) for x in (used, duration, reset)):
            return "unavailable"
        if not 0 <= used <= 100 or duration <= 0 or reset <= 0 or not -62135596800 <= reset - duration <= now:
            return "unavailable"
        # Bound reset to a representable UTC timestamp for response headers.
        if reset >= 253402300799:
            return "unavailable"
    if now - snapshot["fetched_at"] > 3 * interval or any(snapshot[k]["reset_at"] <= now for k in WINDOWS):
        return "stale"
    return "current"


def window_users(tokens, factors, used, threshold, enabled):
    """tokens includes every observed identity and the current caller (possibly zero)."""
    total = sum(tokens.values())
    active = [factors.get(user, 1) for user in tokens if factors.get(user, 1) > 0]
    weight = math.fsum(active)
    users = {}
    for user, count in tokens.items():
        factor = factors.get(user, 1)
        share = count * used / total if total and used is not None else 0.0
        allowance = 100 * factor / weight if weight else 0.0
        users[user] = dict(user_id=user, tokens=count, factor=factor, share=share, allowance=allowance,
                           throttled=bool(enabled and used is not None and used >= threshold
                                          and factor > 0 and len(active) > 1 and share >= allowance))
    return users


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def window_name(window):
    if window["id"] == "secondary":
        return "weekly"
    return "5-hour" if abs(window["window_seconds"] - 18000) <= 60 else "short"
