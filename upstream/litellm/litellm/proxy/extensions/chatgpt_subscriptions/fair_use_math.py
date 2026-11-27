"""Quota validity and weighted shares. All percentages are in percentage points."""
import math
from datetime import datetime, timezone

WINDOWS = ("primary", "secondary")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def active_windows(snapshot):
    """Window ids the plan reports. Some plans only report a weekly primary window."""
    if not isinstance(snapshot, dict):
        return ()
    return tuple(name for name in WINDOWS if snapshot.get(name) is not None)


def quota_state(snapshot, now, interval):
    if not isinstance(snapshot, dict) or not _number(snapshot.get("fetched_at")):
        return "unavailable"
    if snapshot["fetched_at"] > now:
        return "unavailable"
    names = active_windows(snapshot)
    if not names:
        return "unavailable"
    for name in names:
        window = snapshot[name]
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
    if now - snapshot["fetched_at"] > 3 * interval or any(snapshot[k]["reset_at"] <= now for k in names):
        return "stale"
    return "current"


def window_users(measurements, factors, used, threshold, enabled):
    """Each active identity has a (raw tokens, weighted consumption) measurement."""
    total = math.fsum(consumption for _, consumption in measurements.values())
    active = [factors.get(user, 1) for user in measurements if factors.get(user, 1) > 0]
    weight = math.fsum(active)
    users = {}
    for user, (tokens, consumption) in measurements.items():
        factor = factors.get(user, 1)
        share = (consumption / total) * used if total and used is not None else 0.0
        allowance = 100 * factor / weight if weight else 0.0
        users[user] = dict(user_id=user, tokens=tokens, consumption=consumption, factor=factor, share=share, allowance=allowance,
                           throttled=bool(enabled and used is not None and used >= threshold
                                          and factor > 0 and len(active) > 1 and share >= allowance))
    return users


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def window_name(window_id, seconds):
    """Name a window by its duration, because plans assign the ids differently."""
    if not _number(seconds) or seconds <= 0:
        return "weekly" if window_id == "secondary" else "5-hour"
    if abs(seconds - 18000) <= 60:
        return "5-hour"
    if abs(seconds - 604800) <= 60:
        return "weekly"
    days = round(seconds / 86400)
    if days and abs(seconds - days * 86400) <= 60:
        return f"{days}-day"
    return f"{max(1, round(seconds / 3600))}-hour"
