"""Persistent sign-in fences apply across independent worker instances."""

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from litellm.proxy.extensions.chatgpt_subscriptions import accounts, tokens


def record(user="synthetic-user"):
    body = {"exp": int(time.time()) + 3600, "email": user + "@example.test", "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-workspace", "chatgpt_user_id": user}}
    value = "e30." + base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=") + ".sig"
    return {"access_token": value, "id_token": value, "refresh_token": "synthetic-refresh"}


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "auth.json"

    async def delayed_flow(self, action, stage):
        reached, release = asyncio.Event(), asyncio.Event()
        class Flow:
            def __init__(self, **kwargs):
                pass
            async def gate(self, here):
                if stage == here:
                    reached.set()
                    await release.wait()
            async def request_code(self):
                await self.gate("code")
                return {"user_code": "SYNTHETIC-CODE", "device_auth_id": "synthetic", "interval": 0}
            async def poll(self, code):
                await self.gate("poll")
                return {"authorization_code": "synthetic", "code_verifier": "synthetic"}
            async def exchange(self, authorization):
                await self.gate("exchange")
                return record()
            async def close(self):
                pass
        first, second = accounts.Account(path=self.path), accounts.Account(path=self.path)
        first.min_poll = 0
        with patch.object(accounts.oauth, "DeviceFlow", Flow):
            first.start_login()
            await asyncio.wait_for(reached.wait(), 3)
            await getattr(second, action)()
            release.set()
            await asyncio.wait_for(first.login_task, 3)
        self.assertFalse(self.path.exists())
        self.assertFalse(first.pending_path.exists())
        status = second.status()
        self.assertEqual(status["state"], "logged_out")
        self.assertIsNone(status["login"])
        self.assertIsNone(status["pending_account"])
        self.assertIsNone(status["error"])

    async def test_cancel_and_logout_fence_late_code_poll_and_exchange(self):
        for action in ("cancel_login", "logout"):
            for stage in ("code", "poll", "exchange"):
                with self.subTest(action=action, stage=stage):
                    await self.delayed_flow(action, stage)

    async def test_cancel_then_restart_waits_for_old_worker_and_keeps_new_code(self):
        old_started, old_release, new_started, new_release = (asyncio.Event() for _ in range(4))
        class Flow:
            count = 0
            def __init__(self, **kwargs):
                Flow.count += 1
                self.old = Flow.count == 1
            async def request_code(self):
                if self.old:
                    old_started.set()
                    await old_release.wait()
                    raise accounts.oauth.OAuthError("Stale failure")
                new_started.set()
                return {"user_code": "NEW-CODE", "interval": 0}
            async def poll(self, code):
                await new_release.wait()
                return {"authorization_code": "synthetic"}
            async def exchange(self, authorization):
                return record("new-user")
            async def close(self):
                pass
        a, b = accounts.Account(path=self.path), accounts.Account(path=self.path)
        a.min_poll = b.min_poll = 0
        with patch.object(accounts.oauth, "DeviceFlow", Flow):
            a.start_login()
            await asyncio.wait_for(old_started.wait(), 3)
            await b.cancel_login()
            b.start_login()
            await asyncio.sleep(0.1)
            self.assertFalse(b.login_task.done())
            old_release.set()
            await asyncio.wait_for(a.login_task, 3)
            await asyncio.wait_for(new_started.wait(), 3)
            for _ in range(100):
                if b.status()["login"]:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(b.status()["login"]["user_code"], "NEW-CODE")
            self.assertIsNone(b.status()["error"])
            new_release.set()
            await asyncio.wait_for(b.login_task, 3)
        self.assertEqual(b.status()["user_id"], "new-user")
        self.assertIsNone(b.status()["error"])

    def group(self):
        return accounts.Accounts([
            accounts.Account("default", path=self.path.parent / "a/auth.json"),
            accounts.Account("other", path=self.path.parent / "b/auth.json"),
        ])

    async def test_confirmation_rechecks_seat_taken_by_other_worker(self):
        first, second = self.group(), self.group()
        a, b = first.get("default"), second.get("other")
        await a._store_login(record("first"))
        await a._store_login(record("second"))
        self.assertIsNotNone(a.status()["pending_account"])
        await b._store_login(record("second"))
        self.assertFalse(await a.confirm_pending())
        self.assertEqual(a.status()["user_id"], "first")
        self.assertEqual(b.status()["user_id"], "second")
        self.assertEqual(a.status()["error"]["code"], accounts.DUPLICATE_ACCOUNT)

    async def test_two_workers_cannot_install_one_seat_into_two_aliases(self):
        first, second = self.group(), self.group()
        a, b = first.get("default"), second.get("other")
        await asyncio.gather(a._store_login(record()), b._store_login(record()))
        self.assertEqual(sum(item.status()["token_valid"] for item in (a, b)), 1)
        denied = a if not a.status()["token_valid"] else b
        self.assertEqual(denied.status()["error"]["code"], accounts.DUPLICATE_ACCOUNT)

    async def test_cancel_or_logout_invalidates_pending_confirmation(self):
        for action in ("cancel_login", "logout"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "auth.json"
                a, b = accounts.Account(path=path), accounts.Account(path=path)
                await a._store_login(record("first"))
                await a._store_login(record("second"))
                self.assertIsNotNone(a.status()["pending_account"])
                await getattr(b, action)()
                self.assertFalse(await a.confirm_pending())
                self.assertFalse(a.pending_path.exists())
                if action == "logout":
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(tokens.identity(tokens.read(path))["user_id"], "first")


if __name__ == "__main__":
    unittest.main()
