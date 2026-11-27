"""Rebuild each quota window once and fold in terminal attempts from all workers."""
import asyncio
import bisect
import json
import math
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from litellm.caching.dual_cache import DualCache

from .access import key_identity, proxy_db
from .fair_use_math import utc

# Deduplicate callback snapshots before counting. The indexed request interval
# also admits requests that span the boundary. The attempt start decides membership.
HISTORY = """
SELECT DISTINCT ON (a->>'attempt_id') a, s."user", s.api_key, s.team_id,
       EXTRACT(EPOCH FROM s."startTime")::double precision AS request_started
FROM "LiteLLM_SpendLogs" s CROSS JOIN LATERAL jsonb_array_elements(
  CASE WHEN jsonb_typeof(metadata->'subscription_attempts') = 'array'
       THEN metadata->'subscription_attempts' ELSE '[]'::jsonb END) a
WHERE s."startTime" < $2::timestamptz AND (s."startTime" >= $1::timestamptz OR s."endTime" >= $1::timestamptz)
  AND COALESCE(LOWER(cache_hit), '') != 'true' AND a->>'subscription_id' = $3
  AND COALESCE(a->>'attempt_id', '') != ''
ORDER BY a->>'attempt_id', (COALESCE(a->>'outcome', '') IN ('success', 'failure', 'cancelled')) DESC,
         a->>'ended_at' DESC NULLS LAST, s.updated_at DESC, s.request_id DESC
"""


def event(attempt, user, team, request_started=None):
    """Store gateway identity, timestamp and measured tokens only."""
    if not user:
        return None
    try:
        started = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        started = request_started
    identifier = attempt.get("attempt_id")
    if started is None or not isinstance(identifier, str) or not identifier:
        return None
    tokens = sum(v for v in (attempt.get("input_tokens"), attempt.get("output_tokens")) if type(v) is int and v >= 0)
    terminal = bool(attempt.get("ended_at")) or attempt.get("outcome") in ("success", "failure", "cancelled")
    return [identifier, started, user, team, tokens, terminal]


@dataclass(repr=False)
class Journal:
    times: list = field(default_factory=list)
    events: list = field(default_factory=list)


@dataclass(repr=False)
class Window:
    start: float
    reset: float
    seen: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)
    local_cursor: int = 0
    redis_cursor: str | None = None

    def add(self, item):
        if not item:
            return
        identifier, started, user, team, tokens, terminal = item
        if not self.start <= started < self.reset:
            return
        previous = self.seen.get(identifier)
        if previous is not None and (previous[1] or not terminal):
            return
        self.seen[identifier] = (tokens, terminal)
        row = self.users.setdefault(user, {"tokens": 0, "team": team, "started": started})
        row["tokens"] += tokens - (previous[0] if previous else 0)
        if started >= row["started"]:
            row.update(team=team, started=started)


class Counters:
    def __init__(self):
        self.cache = DualCache()
        self.lock = threading.RLock()
        self.loads = {}
        self.retention = {}

    def redis(self):
        proxy = sys.modules.get("litellm.proxy.proxy_server")
        shared = getattr(getattr(proxy, "spend_counter_cache", None), "redis_cache", None)
        if shared is not None:
            self.cache.attach_redis_cache(shared)
        return self.cache.redis_cache

    @staticmethod
    def journal_key(account):
        return "chatgpt:attempts:" + account

    def record(self, account, item):
        if not item:
            return
        key = self.journal_key(account)
        redis = self.redis()
        if redis is not None:
            try:
                self.publish(redis, account, [item])
                return
            except Exception:
                # Keep completed calls during a Redis outage. Replay them on recovery.
                pass
        with self.lock:
            journal = self.cache.get_cache(key, local_only=True)
            if journal is None:
                journal = Journal()
                self.cache.set_cache(key, journal, ttl=self.journal_ttl(account), local_only=True)
            times = journal.times
            times.append(max(time.time_ns(), times[-1] + 1 if times else 0))
            journal.events.append(item)

    def journal_ttl(self, account):
        return self.retention.get(account, 8 * 86400)

    def publish(self, redis, account, events):
        key = redis.check_and_fix_namespace(self.journal_key(account))
        with redis.redis_client.pipeline(transaction=False) as pipe:
            for item in events:
                pipe.xadd(key, {"event": json.dumps(item)})
            pipe.expire(key, self.journal_ttl(account))
            pipe.execute()

    def trim(self, account, windows):
        """Discard events that completed before both window starts."""
        oldest = min(w["reset_at"] - w["window_seconds"] for w in windows)
        self.retention[account] = max(1, math.ceil(max(w["window_seconds"] for w in windows))) + 86400
        key = self.journal_key(account)
        redis = self.redis()
        if redis is not None:
            redis.redis_client.xtrim(redis.check_and_fix_namespace(key), minid=str(max(0, int(oldest * 1000))))
        with self.lock:
            journal = self.cache.get_cache(key, local_only=True)
            if journal:
                count = bisect.bisect_left(journal.times, int(oldest * 1e9))
                del journal.times[:count]
                del journal.events[:count]
                # InMemoryCache preserves a key's original deadline on overwrite.
                self.cache.in_memory_cache.delete_cache(key)
                self.cache.set_cache(key, journal, ttl=self.journal_ttl(account), local_only=True)

    def updates(self, account, window):
        key = self.journal_key(account)
        journal = self.cache.get_cache(key, local_only=True)
        redis = self.redis()
        if redis is not None:
            if journal and journal.events:
                self.publish(redis, account, journal.events)
                self.cache.in_memory_cache.delete_cache(key)
            name = redis.check_and_fix_namespace(key)
            since = "(" + window.redis_cursor if window.redis_cursor else str(max(0, int(window.start * 1000)))
            rows = redis.redis_client.xrange(name, min=since)
            for cursor, fields in rows:
                window.add(json.loads(fields.get("event") or fields[b"event"]))
                window.redis_cursor = cursor.decode() if isinstance(cursor, bytes) else cursor
        elif journal:
            offset = bisect.bisect_right(journal.times, window.local_cursor)
            for item in journal.events[offset:]:
                window.add(item)
            if journal.times:
                window.local_cursor = journal.times[-1]

    async def counts(self, account, name, quota):
        reset = quota["reset_at"]
        start = reset - quota["window_seconds"]
        key = f"chatgpt:counts:{account}:{name}:{reset}:{start}"
        with self.lock:
            window = self.cache.get_cache(key, local_only=True)
        if window is None:
            # Coalesce concurrent SQL reads; cancellation of one caller leaves the load intact.
            task_key = (asyncio.get_running_loop(), key)
            task = self.loads.get(task_key)
            if task is None:
                task = asyncio.create_task(self.rebuild(key, account, start, reset))
                self.loads[task_key] = task
                task.add_done_callback(lambda _: self.loads.pop(task_key, None))
            window = await asyncio.shield(task)
        return await asyncio.to_thread(self.snapshot, account, window)

    async def rebuild(self, key, account, start, reset):
        window = Window(start, reset)
        db = proxy_db()
        if db is not None:
            rows = await db.db.query_raw(HISTORY, utc(start), utc(reset), account)
            for row in rows:
                window.add(event(row["a"], row["user"] or key_identity(row["api_key"]), row["team_id"],
                                 row.get("request_started")))
        with self.lock:
            self.cache.set_cache(key, window, ttl=max(1, math.ceil(reset - time.time())), local_only=True)
        return window

    def snapshot(self, account, window):
        with self.lock:
            self.updates(account, window)
            return {user: row.copy() for user, row in window.users.items()}


counters = Counters()
