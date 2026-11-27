"""Cached provider windows, quota evaluation and public status projections."""
import asyncio
import time

from .access import Caller, Grants
from .accounts import _env_seconds
from .counters import counters
from .fair_use_math import WINDOWS, quota_state, utc, window_users
from .service import UsageService


class Quotas:
    def __init__(self):
        self.snapshots = {}
        self.refreshed_at = None

    @property
    def interval(self):
        return _env_seconds("CHATGPT_FAIR_USE_REFRESH_SECONDS", 60)

    async def refresh(self, account):
        status = await asyncio.to_thread(account.status)
        identifier = status["subscription_id"]
        if status["retired"] or not status["token_valid"]:
            self.snapshots.pop(identifier, None)
            return
        service = UsageService(account=account.name, path=account.path, transport=account.transport)
        try:
            quota = await asyncio.wait_for(service.fetch(codex_only=True), timeout=10)
            snapshot = {"fetched_at": quota["fetched_at"], **quota["limits"][0]}
            # Drop a result if the credential changed while the HTTP call was in flight.
            current = await asyncio.to_thread(account.status)
            if not current["token_valid"] or current["connection_revision"] != status["connection_revision"]:
                self.snapshots.pop(identifier, None)
                return
            previous = self.snapshots.get(identifier) or {}
            self.snapshots[identifier] = snapshot
            if quota_state(snapshot, time.time(), self.interval) == "current":
                await asyncio.to_thread(counters.trim, identifier, [snapshot[w] for w in WINDOWS])
                for name in WINDOWS:
                    old = previous.get(name)
                    if old and old.get("reset_at") != snapshot[name]["reset_at"]:
                        await counters.counts(identifier, name, snapshot[name])
        except Exception:
            self.snapshots.pop(identifier, None)

    async def watch(self, accounts):
        while True:
            start = time.monotonic()
            try:
                accounts_now = await asyncio.to_thread(list, accounts)
                await asyncio.gather(*(self.refresh(a) for a in accounts_now), return_exceptions=True)
            except (OSError, ValueError):
                # An unreadable registry must not permanently stop subsequent refreshes.
                pass
            self.refreshed_at = time.time()
            await asyncio.sleep(max(0, self.interval - (time.monotonic() - start)))


quotas = Quotas()


async def evaluate(record, factors, caller=None):
    """Return the same measurements for routing, admin status and the caller view."""
    account = record["subscription_id"]
    policy = record["fair_use"]
    snapshot = quotas.snapshots.get(account)
    state = quota_state(snapshot, time.time(), quotas.interval)
    counts = {name: {} for name in WINDOWS}
    if state == "current":
        try:
            for name in WINDOWS:
                counts[name] = await counters.counts(account, name, snapshot[name])
        except Exception:
            # A partial rebuild cannot supply a fair share. Retry on the next read.
            counts = {name: {} for name in WINDOWS}
            state = "unavailable"

    contexts = {
        name: {user: Caller(None if user.startswith("key:") else user, row["team"]) for user, row in rows.items()}
        for name, rows in counts.items()
    }
    active = [identity for users in contexts.values() for identity in users.values()]
    if caller:
        active.append(caller)
    grants = await Grants.resolve([], factors, active)
    windows = []
    for name, rows in counts.items():
        quota = snapshot[name] if state == "current" else {}
        used = quota.get("used_percent")
        threshold = policy[name + "_threshold"]
        tokens = {user: row["tokens"] for user, row in rows.items()}
        weights = {user: grants.factor(identity) for user, identity in contexts[name].items()}
        if caller and caller.identity:
            tokens.setdefault(caller.identity, 0)
            weights[caller.identity] = grants.factor(caller)
        users = window_users(tokens, weights, used, threshold, policy["enabled"])
        windows.append(dict(id=name, threshold=threshold, used_percent=used, reset_at=quota.get("reset_at"),
                            window_seconds=quota.get("window_seconds"), scarce=used is not None and used >= threshold,
                            users=users))
    return state, windows


def response_headers(windows, user):
    if not windows or not user:
        return {}
    window = min(windows, key=lambda w: w["users"][user]["allowance"] - w["users"][user]["share"])
    row = window["users"][user]
    return {"x-litellm-fair-use-share": f'{row["share"]:.1f}',
            "x-litellm-fair-use-allowance": f'{row["allowance"]:.1f}',
            "x-litellm-fair-use-reset": utc(window["reset_at"])}


def dispatched_headers(logging_obj):
    headers = getattr(logging_obj, "_subscription_fair_use_headers", None)
    if isinstance(headers, dict) and headers and time.time() < logging_obj._subscription_headers_until:
        return headers
    return {}
