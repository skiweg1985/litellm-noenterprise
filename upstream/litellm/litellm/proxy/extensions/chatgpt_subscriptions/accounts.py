"""Keep the ChatGPT sign-in alive without blocking model requests.

A background task refreshes the token before it expires and, when the
refresh token is gone or rejected, runs the device-code sign-in. Its state
lives in "<auth file>.state.json" so every worker, PRTG and the admin page
see the same thing.

The account remembers who it belongs to (workspace and person), taken from
the first valid token, until an admin signs it out. A sign-in with anyone else is held back in
"<auth file>.pending.json" until an admin confirms or discards it, so a
leaked device code cannot silently switch the subscription.

Accounts holds one Account per name in CHATGPT_ACCOUNTS. A person signed in
to one subscription is refused for all others: the same seat twice would
only share one quota.
"""

import asyncio
import contextlib
import fcntl
import hashlib
import os
import secrets
import time
from pathlib import Path

from . import oauth, tokens

# Error codes; the admin page translates them, PRTG shows the message.
UNREACHABLE = "unreachable"
REFRESH_REJECTED = "refresh_rejected"
WRONG_ACCOUNT = "wrong_account"
DUPLICATE_ACCOUNT = "duplicate_account"
LOGIN_FAILED = "login_failed"
NO_REFRESH_TOKEN = "no_refresh_token"
BUSY = "busy"


def _env_seconds(name, default):
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        return default


def _fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None


class Account:
    def __init__(self, name=tokens.DEFAULT_ACCOUNT, path=None, transport=None, clock=time.time):
        self.name = name
        self.path = Path(path or tokens.auth_path(name))
        self.registry_path = self.path.parent / ".subscriptions"
        self.state_path = self.path.with_name(self.path.name + ".state.json")
        self.pending_path = self.path.with_name(self.path.name + ".pending.json")
        self.transport = transport
        self.clock = clock
        self.auto_login = os.getenv("CHATGPT_AUTO_LOGIN", "false").lower() != "false"
        self.expected_account = os.getenv("CHATGPT_EXPECTED_ACCOUNT_ID") or None
        self.refresh_before = _env_seconds("CHATGPT_REFRESH_BEFORE_SECONDS", 6 * 3600)
        self.min_poll = _env_seconds("CHATGPT_DEVICE_POLL_MIN_SECONDS", 5)
        self.login_task = None
        self.wake = asyncio.Event()
        self.siblings = lambda: []  # the other accounts, set by Accounts

    # -- persisted state -------------------------------------------------

    def _state(self):
        return tokens.read(self.state_path) or {}

    def _update(self, **changes):
        with tokens.locked(self.state_path):
            state = self._state()
            state.update(changes)
            tokens.write(state, self.state_path)

    def _fail(self, code, message):
        self._update(error={"code": code, "message": message, "at": self.clock()})

    # -- status ----------------------------------------------------------

    def status(self):
        record = tokens.read(self.path)
        state = self._state()
        now = self.clock()
        expiry = tokens.expires_at(record)
        valid = bool(record and record.get("access_token") and expiry and expiry > now + 60)
        refresh = (record or {}).get("refresh_token")
        rejected = bool(refresh) and _fingerprint(refresh) == state.get("rejected_refresh")
        pending = state.get("pending_identity") if (
            self.pending_path.exists() and not state.get("suspended")
            and state.get("pending_generation") == state.get("login_generation")
        ) else None
        login = state.get("login")
        if login and login.get("expires_at", 0) <= now:
            login = None
        if login:
            current = "login_pending"
        elif valid:
            current = "ok"
        elif state.get("suspended"):
            current = "logged_out"
        elif refresh and not rejected:
            current = "token_expired"
        else:
            current = "login_required"
        return {
            "account": self.name,
            "state": current,
            # A voluntary new sign-in can run while the current token still works.
            "token_valid": valid,
            **tokens.identity(record if record and record.get("access_token") else None),
            "token_expires_at": expiry if record else None,
            "last_refresh_at": state.get("last_refresh_at"),
            "refresh_rejected": rejected,
            "login": login,
            "error": state.get("error"),
            "auto_login": self.auto_login and not state.get("suspended"),
            "known_account": state.get("known_identity"),
            "pending_account": pending,
        }

    def identities(self):
        """Who this account belongs to and who its current token names."""
        found = [self._state().get("known_identity")]
        record = tokens.read(self.path)
        if record and record.get("access_token"):
            found.append(tokens.identity(record))
        return [identity for identity in found if identity]

    def _taken_by(self, signed_in):
        for other in self.siblings():
            if any(tokens.same_identity(known, signed_in) for known in other.identities()):
                return other.name
        return None

    def _remember_identity(self):
        """Trust on first use: the first valid token names the account."""
        with tokens.locked(self.registry_path), tokens.locked(self.path):
            if self._state().get("known_identity"):
                return
            record = tokens.read(self.path)
            expiry = tokens.expires_at(record)
            if record and record.get("access_token") and expiry and expiry > self.clock():
                known = tokens.identity(record)
                if known["account_id"] and (known["user_id"] or known["email"]):
                    self._update(known_identity=known)

    # -- refresh ---------------------------------------------------------

    def _refresh_blocking(self, force=False):
        """Refresh under the file lock; skip if another process just did."""
        try:
            with tokens.locked(self.path):
                record = tokens.read(self.path)
                refresh = (record or {}).get("refresh_token")
                if not refresh:
                    self._fail(NO_REFRESH_TOKEN, "No refresh token. A new sign-in is required.")
                    return
                if _fingerprint(refresh) == self._state().get("rejected_refresh"):
                    return
                expiry = tokens.expires_at(record)
                if not force and expiry and expiry - self.clock() > self.refresh_before:
                    return
                try:
                    new = oauth.refresh(refresh, transport=self.transport)
                except oauth.OAuthError as exc:
                    if exc.permanent:
                        self._update(rejected_refresh=_fingerprint(refresh))
                        self._fail(REFRESH_REJECTED, str(exc))
                    else:
                        self._fail(UNREACHABLE, str(exc))
                    return
                tokens.write(tokens.build_record(new, record), self.path)
                self._update(last_refresh_at=self.clock(), error=None, rejected_refresh=None)
        except tokens.LockTimeout:
            self._fail(BUSY, "Token file is locked by another process.")

    # -- device sign-in --------------------------------------------------

    def _login_running(self):
        return self.login_task is not None and not self.login_task.done()

    def _flow_update(self, generation, **changes):
        """Publish only for the still-current flow, including after remote cancellation."""
        with tokens.locked(self.path):
            state = self._state()
            if state.get("login_generation") != generation or state.get("suspended"):
                return False
            self._update(**changes)
            return True

    def _begin_flow(self, epoch, automatic, generation):
        with tokens.locked(self.path):
            state = self._state()
            if state.get("login_epoch", 0) != epoch or (automatic and state.get("suspended")):
                return False
            self._update(login_generation=generation, suspended=False, login=None)
            return True

    async def _publish_flow(self, generation, **changes):
        return await asyncio.to_thread(self._flow_update, generation, **changes)

    async def _run_login(self, epoch, automatic):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = os.open(f"{self.path}.login.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    state = self._state()
                    if state.get("login_epoch", 0) != epoch or automatic or not state.get("suspended"):
                        return  # another current flow is already serving this request
                    # A cancelled worker may still be in an upstream HTTP call.
                    # Keep the new explicit request until that old worker releases its lock.
                    await asyncio.sleep(0.05)
            generation = secrets.token_hex(16)
            if not await asyncio.to_thread(self._begin_flow, epoch, automatic, generation):
                return
            flow = oauth.DeviceFlow(transport=self.transport)
            try:
                code = await flow.request_code()
                deadline = self.clock() + oauth.CODE_VALIDITY_SECONDS
                if not await self._publish_flow(
                    generation,
                    login={
                        "user_code": code["user_code"],
                        "verification_url": oauth.verification_url(),
                        "expires_at": deadline,
                    },
                ):
                    return
                while self.clock() < deadline:
                    if not await self._publish_flow(generation):
                        return
                    await asyncio.sleep(max(self.min_poll, code["interval"]))
                    authorization = await flow.poll(code)
                    if authorization:
                        if not await self._publish_flow(generation):
                            return
                        await self._store_login(await flow.exchange(authorization), generation)
                        return
            except oauth.OAuthError as exc:
                await self._publish_flow(generation, error={"code": LOGIN_FAILED, "message": str(exc), "at": self.clock()})
            finally:
                await flow.close()
                await self._publish_flow(generation, login=None)
        finally:
            os.close(lock)

    def _store_login_blocking(self, new, generation):
        # Every seat assignment and removal uses registry -> token -> state.
        # Request-time refresh needs only the token lock and cannot change ownership.
        with tokens.locked(self.registry_path), tokens.locked(self.path):
            state = self._state()
            if state.get("login_generation") != generation or state.get("suspended"):
                return
            record = tokens.build_record(new)
            signed_in = tokens.identity(record)
            if self.expected_account and signed_in["account_id"] != self.expected_account:
                self._fail(WRONG_ACCOUNT, "Signed in with a different ChatGPT workspace. The tokens were discarded.")
                return
            taken = self._taken_by(signed_in)
            if taken:
                self._fail(DUPLICATE_ACCOUNT, f"This ChatGPT account is already signed in as subscription {taken}. The tokens were discarded.")
                return
            known = state.get("known_identity")
            if known and not tokens.same_identity(known, signed_in):
                tokens.write(record, self.pending_path)
                self._update(pending_identity={**signed_in, "at": self.clock()}, pending_generation=generation)
                return
            tokens.write(record, self.path)
            tokens.remove(self.pending_path)
            self._update(
                known_identity=signed_in, pending_identity=None, pending_generation=None,
                last_refresh_at=self.clock(), error=None, rejected_refresh=None, suspended=False,
            )

    async def _store_login(self, new, generation=None):
        if generation is None:
            generation = self._state().get("login_generation")
        await asyncio.to_thread(self._store_login_blocking, new, generation)

    # -- actions ---------------------------------------------------------

    async def check(self):
        if self._login_running():
            return
        await asyncio.to_thread(self._remember_identity)
        record = tokens.read(self.path)
        if (record or {}).get("refresh_token"):
            await asyncio.to_thread(self._refresh_blocking)
        status = self.status()
        needs_login = status["state"] == "login_required" or (
            status["state"] == "ok" and status["refresh_rejected"]
        )
        # A held-back sign-in waits for an admin; no new codes meanwhile.
        if needs_login and status["auto_login"] and not status["pending_account"]:
            self.start_login(automatic=True)

    def start_login(self, automatic=False):
        if not self._login_running():
            epoch = self._state().get("login_epoch", 0)
            self.login_task = asyncio.ensure_future(self._run_login(epoch, automatic))

    def _invalidate_login(self, remove_credentials=False):
        with tokens.locked(self.registry_path), tokens.locked(self.path):
            epoch = self._state().get("login_epoch", 0)
            self._update(login_epoch=epoch + 1, login_generation=None, suspended=True,
                         login=None, pending_identity=None, pending_generation=None)
            tokens.remove(self.pending_path)
            if remove_credentials:
                tokens.remove(self.path)
                self._update(error=None, rejected_refresh=None, last_refresh_at=None, known_identity=None)

    async def _cancel_local_task(self):
        if self._login_running():
            self.login_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.login_task

    async def cancel_login(self):
        await asyncio.to_thread(self._invalidate_login)
        await self._cancel_local_task()

    def _confirm_blocking(self):
        with tokens.locked(self.registry_path), tokens.locked(self.path):
            state = self._state()
            if state.get("suspended") or state.get("pending_generation") != state.get("login_generation"):
                return False
            record = tokens.read(self.pending_path)
            if not record:
                return False
            signed_in = tokens.identity(record)
            taken = self._taken_by(signed_in)
            if taken:
                self._fail(DUPLICATE_ACCOUNT, f"This ChatGPT account is already signed in as subscription {taken}. Confirmation was refused.")
                return False
            tokens.write(record, self.path)
            tokens.remove(self.pending_path)
            self._update(
                known_identity=signed_in, pending_identity=None, pending_generation=None,
                last_refresh_at=self.clock(), error=None, rejected_refresh=None, suspended=False,
            )
        return True

    async def confirm_pending(self):
        """Confirm only a current, non-conflicting pending sign-in."""
        return await asyncio.to_thread(self._confirm_blocking)

    def _discard_pending(self):
        with tokens.locked(self.registry_path), tokens.locked(self.path):
            tokens.remove(self.pending_path)
            self._update(pending_identity=None, pending_generation=None)

    async def discard_pending(self):
        await asyncio.to_thread(self._discard_pending)

    async def refresh_now(self):
        await asyncio.to_thread(self._refresh_blocking, True)

    async def logout(self):
        """Invalidate all workers' sign-ins before atomically removing credentials."""
        await asyncio.to_thread(self._invalidate_login, True)
        await self._cancel_local_task()

    async def run(self, interval=None):
        interval = interval or _env_seconds("CHATGPT_WATCH_INTERVAL_SECONDS", 60)
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - the watcher must survive any single failure
                self._fail(LOGIN_FAILED, "Unexpected error in the ChatGPT token watcher.")
            self.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), interval)


class Accounts:
    """The configured subscriptions, by name."""

    def __init__(self, items=None, transport=None, clock=time.time):
        if items is None:
            items = [Account(name, transport=transport, clock=clock) for name in tokens.configured_accounts()]
        self.items = {account.name: account for account in items}
        registry = Path(os.path.commonpath([a.path.parent for a in self.items.values()])) / ".subscriptions" if self.items else None
        for account in self.items.values():
            account.registry_path = registry
            account.siblings = lambda me=account: [a for a in self.items.values() if a is not me]

    def __iter__(self):
        return iter(self.items.values())

    def __len__(self):
        return len(self.items)

    def get(self, name):
        return self.items.get(name)

    async def run(self, interval=None):
        await asyncio.gather(*(account.run(interval) for account in self))

    async def stop_logins(self):
        tasks = [account.login_task for account in self if account.login_task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
