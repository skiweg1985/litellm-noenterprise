import asyncio
import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from litellm.proxy.extensions.chatgpt_subscriptions import accounts, admin, tokens
from litellm.proxy.extensions.chatgpt_subscriptions import accounts as accounts_module
from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Account, Accounts
from litellm.proxy.extensions.chatgpt_subscriptions.routes import register


def jwt(**claims):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def access(exp, account="acct-1", email="team@example.com", serial=0, user="user-1"):
    return jwt(
        exp=exp,
        serial=serial,
        email=email,
        **{"https://api.openai.com/auth": {
            "chatgpt_account_id": account, "chatgpt_user_id": user, "chatgpt_plan_type": "team"}},
    )


class FakeAuth:
    """OpenAI's sign-in endpoints; refresh tokens rotate like the real ones."""

    def __init__(self, account="acct-1"):
        self.account = account
        self.email = "team@example.com"
        self.user = "user-1"
        self.refresh_token = "refresh-1"
        self.calls = []
        self.fail = None
        self.polls_until_confirmed = 1
        self.serial = 0

    def issue(self):
        self.serial += 1
        self.refresh_token = f"refresh-{self.serial + 1}"
        token = access(time.time() + 10 * 86400, self.account, self.email, self.serial, self.user)
        return {"access_token": token, "id_token": token, "refresh_token": self.refresh_token}

    def __call__(self, request):
        path = request.url.path
        self.calls.append(path)
        if self.fail == "network":
            raise httpx.ConnectError("down", request=request)
        if self.fail == "server":
            return httpx.Response(500, text="secret-body")
        if path == "/oauth/token" and request.headers["content-type"].startswith("application/json"):
            body = json.loads(request.content)
            if self.fail == "reject" or body["refresh_token"] != self.refresh_token:
                return httpx.Response(401, json={"error": "refresh_token_reused"})
            return httpx.Response(200, json=self.issue())
        if path == "/api/accounts/deviceauth/usercode":
            return httpx.Response(200, json={"device_auth_id": "dev-1", "user_code": "ABCD-EFGH", "interval": 0})
        if path == "/api/accounts/deviceauth/token":
            self.polls_until_confirmed -= 1
            if self.polls_until_confirmed >= 0:
                return httpx.Response(403)
            return httpx.Response(200, json={"authorization_code": "code", "code_challenge": "c", "code_verifier": "v"})
        if path == "/oauth/token":
            return httpx.Response(200, json=self.issue())
        return httpx.Response(404)


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(
            os.environ,
            {
                "CHATGPT_TOKEN_DIR": self.tmp.name,
                "CHATGPT_AUTH_FILE": "auth.json",
                "CHATGPT_AUTH_BASE": "https://auth.test",
                "CHATGPT_MONITORING_API_KEY": "monitor-secret",
                "CHATGPT_AUTO_LOGIN": "true",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.path = Path(self.tmp.name) / "auth.json"
        self.fake = FakeAuth()

    def account(self, **kwargs):
        account = Account(transport=httpx.MockTransport(self.fake), **kwargs)
        account.min_poll = 0.01
        return account

    def store(self, expires_in, refresh="refresh-1"):
        token = access(time.time() + expires_in)
        tokens.write(
            {"access_token": token, "id_token": token, "refresh_token": refresh,
             "expires_at": time.time() + expires_in, "account_id": "acct-1"},
            self.path,
        )

    async def settle(self, account):
        previous = account.login_task
        await account.check()
        if account.login_task is not previous:
            await asyncio.wait_for(account.login_task, 5)

    # -- token file --------------------------------------------------------

    def test_write_is_atomic_private_and_reentrant(self):
        with tokens.locked(self.path):
            with tokens.locked(self.path):
                tokens.write({"a": 1}, self.path)
        self.assertEqual(tokens.read(self.path), {"a": 1})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.startswith(".auth")], [])
        self.path.write_text("{broken")
        self.assertIsNone(tokens.read(self.path))

    def test_lock_excludes_other_processes(self):
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT); "
             "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(5)",
             f"{self.path}.lock"],
            stdout=subprocess.PIPE, text=True,
        )

        def stop():
            holder.kill()
            holder.wait()
            holder.stdout.close()

        self.addCleanup(stop)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        with self.assertRaises(tokens.LockTimeout):
            with tokens.locked(self.path, timeout=0.2):
                pass

    # -- refresh -----------------------------------------------------------

    def test_refresh_only_when_due(self):
        self.store(expires_in=30 * 86400)
        account = self.account()
        asyncio.run(self.settle(account))
        self.assertEqual(self.fake.calls, [])
        self.store(expires_in=3600)
        asyncio.run(self.settle(account))
        self.assertEqual(self.fake.calls, ["/oauth/token"])
        record = tokens.read(self.path)
        self.assertEqual(record["refresh_token"], "refresh-2")
        self.assertEqual(record["account_id"], "acct-1")
        status = account.status()
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["email"], "team@example.com")
        self.assertIsNotNone(status["last_refresh_at"])

    def test_transient_failure_keeps_account_and_does_not_sign_in(self):
        self.store(expires_in=3600)
        for failure in ("network", "server"):
            self.fake.fail = failure
            account = self.account()
            asyncio.run(self.settle(account))
            status = account.status()
            self.assertEqual(status["state"], "ok")
            self.assertEqual(status["error"]["code"], accounts.UNREACHABLE)
            self.assertNotIn("secret-body", json.dumps(status))
            self.assertNotIn("/api/accounts/deviceauth/usercode", self.fake.calls)

    def test_rejected_refresh_starts_sign_in_and_stores_new_tokens(self):
        self.store(expires_in=60 * 30)
        self.fake.fail = "reject"
        account = self.account()

        async def run():
            await account.check()
            self.assertIsNotNone(account.login_task)
            for _ in range(200):
                status = account.status()
                if status["state"] == "login_pending":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(status["login"]["user_code"], "ABCD-EFGH")
            self.assertEqual(status["login"]["verification_url"], "https://auth.test/codex/device")
            self.fake.fail = None
            await asyncio.wait_for(account.login_task, 5)

        asyncio.run(run())
        status = account.status()
        self.assertEqual(status["state"], "ok")
        self.assertIsNone(status["error"])
        self.assertFalse(status["refresh_rejected"])
        self.assertEqual(tokens.read(self.path)["refresh_token"], self.fake.refresh_token)
        # The rejected token is not retried on every check.
        calls = len(self.fake.calls)
        asyncio.run(self.settle(account))
        self.assertEqual(len(self.fake.calls), calls)

    def test_missing_token_signs_in_and_expired_code_is_replaced(self):
        account = self.account()
        self.assertEqual(account.status()["state"], "login_required")
        self.fake.polls_until_confirmed = 10**6
        with patch.object(accounts.oauth, "CODE_VALIDITY_SECONDS", 0.05):
            asyncio.run(self.settle(account))
        self.assertEqual(account.status()["state"], "login_required")
        self.fake.polls_until_confirmed = 0
        asyncio.run(self.settle(account))
        self.assertEqual(account.status()["state"], "ok")
        self.assertEqual(self.fake.calls.count("/api/accounts/deviceauth/usercode"), 2)

    def test_wrong_account_is_rejected(self):
        with patch.dict(os.environ, {"CHATGPT_EXPECTED_ACCOUNT_ID": "acct-expected"}):
            account = self.account()
        asyncio.run(self.settle(account))
        self.assertFalse(self.path.exists())
        status = account.status()
        self.assertEqual(status["error"]["code"], accounts.WRONG_ACCOUNT)
        self.assertEqual(status["state"], "login_required")

    def test_auto_login_can_be_disabled(self):
        with patch.dict(os.environ, {"CHATGPT_AUTO_LOGIN": "false"}):
            account = self.account()
        asyncio.run(self.settle(account))
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(account.status()["auto_login"])

    def test_cancel_and_logout_suspend_automatic_sign_in(self):
        self.store(expires_in=30 * 86400)
        account = self.account()

        async def run():
            self.fake.polls_until_confirmed = 10**6
            account.start_login()
            await asyncio.sleep(0.05)
            self.assertEqual(account.status()["state"], "login_pending")
            await account.cancel_login()
            self.assertEqual(account.status()["state"], "ok")
            await account.logout()
            await account.check()

        asyncio.run(run())
        self.assertFalse(self.path.exists())
        self.assertEqual(account.status()["state"], "logged_out")
        self.assertIsNone(account.login_task.result() if account.login_task.done() and not account.login_task.cancelled() else None)
        self.assertEqual(self.fake.calls.count("/api/accounts/deviceauth/usercode"), 1)

    def test_only_one_process_runs_the_sign_in(self):
        first, second = self.account(), self.account()
        self.fake.polls_until_confirmed = 10**6

        async def run():
            first.start_login()
            await asyncio.sleep(0.05)
            second.start_login()
            await asyncio.wait_for(second.login_task, 5)
            self.assertEqual(second.status()["state"], "login_pending")
            first.login_task.cancel()

        asyncio.run(run())
        self.assertEqual(self.fake.calls.count("/api/accounts/deviceauth/usercode"), 1)

    # -- account identity --------------------------------------------------

    def test_first_valid_token_names_the_account(self):
        self.store(expires_in=30 * 86400)
        account = self.account()
        self.assertIsNone(account.status()["known_account"])
        asyncio.run(self.settle(account))
        known = account.status()["known_account"]
        self.assertEqual((known["account_id"], known["user_id"], known["email"], known["plan"]),
                         ("acct-1", "user-1", "team@example.com", "team"))

    def test_same_person_signs_in_directly(self):
        self.store(expires_in=30 * 86400)
        account = self.account()

        async def run():
            await account.check()
            self.fake.email = "renamed@example.com"  # same person, new address
            account.start_login()
            await asyncio.wait_for(account.login_task, 5)

        asyncio.run(run())
        status = account.status()
        self.assertIsNone(status["pending_account"])
        self.assertEqual(status["email"], "renamed@example.com")
        self.assertEqual(status["known_account"]["email"], "renamed@example.com")

    def test_different_person_is_held_back_until_confirmed(self):
        self.store(expires_in=30 * 86400)
        before = tokens.read(self.path)
        account = self.account()
        self.fake.user, self.fake.email = "user-2", "colleague@example.com"

        async def run():
            await account.check()
            account.start_login()
            await asyncio.wait_for(account.login_task, 5)

        asyncio.run(run())
        status = account.status()
        self.assertEqual(tokens.read(self.path), before)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["pending_account"]["email"], "colleague@example.com")
        self.assertEqual(stat.S_IMODE(account.pending_path.stat().st_mode), 0o600)
        client = self.client(account)
        sensor = client.get("/chatgpt/accounts/default/usage/prtg?windows=primary",
                            headers={"X-Monitoring-Key": "monitor-secret"}).json()["prtg"]
        self.assertEqual(sensor["error"], 1)
        self.assertIn("different ChatGPT account", sensor["text"])
        self.assertNotIn("colleague@example.com", sensor["text"])
        data = client.post("/chatgpt/api/accounts/default/pending/confirm").json()
        self.assertEqual((data["email"], data["known_account"]["user_id"]), ("colleague@example.com", "user-2"))
        self.assertIsNone(data["pending_account"])
        self.assertFalse(account.pending_path.exists())
        self.assertEqual(client.post("/chatgpt/api/accounts/default/pending/confirm").status_code, 409)

    def test_discarded_sign_in_keeps_the_account_and_stops_new_codes(self):
        self.store(expires_in=-10, refresh="gone")
        account = self.account()
        with tokens.locked(account.state_path):
            tokens.write({"known_identity": {"account_id": "acct-1", "user_id": "user-1",
                                             "email": "team@example.com", "plan": "team"}}, account.state_path)
        self.fake.user = "user-2"
        self.fake.fail = None
        asyncio.run(self.settle(account))
        self.assertIsNotNone(account.status()["pending_account"])
        codes = self.fake.calls.count("/api/accounts/deviceauth/usercode")
        asyncio.run(self.settle(account))
        self.assertEqual(self.fake.calls.count("/api/accounts/deviceauth/usercode"), codes)
        data = self.client(account).post("/chatgpt/api/accounts/default/pending/discard").json()
        self.assertIsNone(data["pending_account"])
        self.assertFalse(account.pending_path.exists())
        self.assertEqual(data["known_account"]["user_id"], "user-1")

    def test_other_workspace_member_is_not_the_same_account(self):
        base = {"account_id": "acct-1", "user_id": "user-1", "email": "a@example.com"}
        self.assertTrue(tokens.same_identity(base, {**base, "email": "new@example.com"}))
        self.assertFalse(tokens.same_identity(base, {**base, "user_id": "user-2"}))
        self.assertFalse(tokens.same_identity(base, {**base, "account_id": "acct-2"}))
        self.assertTrue(tokens.same_identity({**base, "user_id": None}, {**base, "user_id": None, "email": "A@example.com"}))

    # -- routes ------------------------------------------------------------

    def client(self, account, allowed=True, deployments=None):
        app = FastAPI()
        accounts = account if isinstance(account, Accounts) else Accounts([account])
        deployments = deployments or [
            {"model_name": "gpt-sub", "slug": "gpt-6-luna", "id": "dep-1", "account": "default", "credential": None, "problem": None},
            {"model_name": "gpt-old", "slug": "gpt-4-retired", "id": "dep-4", "account": "default", "credential": None, "problem": None},
        ]

        def dependency():
            if not allowed:
                raise HTTPException(403, "Proxy admin role required.")

        class Usage:
            async def get(self):
                return {"account": "default", "fetched_at": time.time(), "limits": [
                    {"id": "codex", "allowed": True,
                     "primary": {"used_percent": 5, "remaining_percent": 95, "window_seconds": 18000, "reset_at": time.time() + 600},
                     "secondary": None}]}

        class Catalog:
            async def get(self):
                entry = {"context_window": 272000, "reasoning": ["low", "medium"]}
                return {"account": "default", "fetched_at": time.time(), "models": [
                    {**entry, "slug": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "visible": True, "priority": 0},
                    {**entry, "slug": "gpt-6-luna", "name": "GPT-6-Luna", "visible": True, "priority": 4},
                    {**entry, "slug": "gpt-reserve", "name": "GPT-Reserve", "visible": False, "priority": 4}]}

        async def requests():
            return {"dep-1": 4, "dep-2": 3, "dep-3": 5}

        with patch.object(admin, "RequestCounter", lambda: requests), \
                patch.object(admin, "chatgpt_deployments", lambda: deployments):
            register(app, accounts=accounts, services={a.name: Usage() for a in accounts},
                     catalogs={a.name: Catalog() for a in accounts}, admin_dependency=dependency, watch=False)
        return TestClient(app)

    def test_prtg_reports_pending_sign_in(self):
        account = self.account()
        with tokens.locked(account.state_path):
            tokens.write({"login": {"user_code": "ABCD-EFGH", "verification_url": "https://auth.test/codex/device",
                                    "expires_at": time.time() + 600}}, account.state_path)
        result = self.client(account).get(
            "/chatgpt/accounts/default/usage/prtg", headers={"X-Monitoring-Key": "monitor-secret"}
        ).json()["prtg"]
        self.assertEqual(result["error"], 1)
        self.assertIn("Login required", result["text"])
        self.assertNotIn("ABCD-EFGH", json.dumps(result))
        self.assertNotIn("https://auth.test/codex/device", json.dumps(result))
        self.store(expires_in=30 * 86400)
        tokens.remove(account.state_path)
        result = self.client(account).get(
            "/chatgpt/accounts/default/usage/prtg?windows=primary", headers={"X-Monitoring-Key": "monitor-secret"}
        ).json()["prtg"]
        self.assertNotIn("error", result)
        self.assertEqual(result["result"][1]["value"], 95)

    def test_voluntary_sign_in_keeps_prtg_green(self):
        self.store(expires_in=30 * 86400)
        account = self.account()
        with tokens.locked(account.state_path):
            tokens.write({"login": {"user_code": "ABCD-EFGH", "verification_url": "https://auth.test/codex/device",
                                    "expires_at": time.time() + 600}}, account.state_path)
        client = self.client(account)
        result = client.get(
            "/chatgpt/accounts/default/usage/prtg?windows=primary", headers={"X-Monitoring-Key": "monitor-secret"}
        ).json()["prtg"]
        self.assertNotIn("error", result)
        self.assertIn("New sign-in pending", result["text"])
        self.assertNotIn("ABCD-EFGH", json.dumps(result))
        data = client.get("/chatgpt/api/accounts").json()["accounts"][0]
        self.assertEqual((data["state"], data["token_valid"]), ("login_pending", True))
        self.assertIsNotNone(data["quota"])
        # A rejected refresh token alarms even while the current token still works.
        record = tokens.read(self.path)
        with tokens.locked(account.state_path):
            tokens.write({"rejected_refresh": accounts._fingerprint(record["refresh_token"])}, account.state_path)
        result = client.get(
            "/chatgpt/accounts/default/usage/prtg?windows=primary", headers={"X-Monitoring-Key": "monitor-secret"}
        ).json()["prtg"]
        self.assertEqual(result["error"], 1)
        self.assertIn("Login required", result["text"])

    def test_admin_api_and_page(self):
        self.store(expires_in=30 * 86400)
        account = self.account()
        client = self.client(account)
        page = client.get("/chatgpt/ui", follow_redirects=False)
        self.assertEqual(page.status_code, 307)
        self.assertEqual(page.headers["location"], "/ui/chatgpt-subscriptions/")
        data = client.get("/chatgpt/api/accounts").json()["accounts"][0]
        self.assertEqual((data["state"], data["requests_24h"], data["models"]), ("ok", 4, ["gpt-old", "gpt-sub"]))
        available = data["available_models"]
        self.assertEqual([(m["slug"], m["configured"]) for m in available["models"]],
                         [("gpt-6.1-sol", False), ("gpt-6-luna", True)])
        self.assertEqual(available["missing"], [{"model_name": "gpt-old", "slug": "gpt-4-retired"}])
        self.assertIsNone(data["available_models_error"])
        self.assertEqual(data["quota"]["limits"][0]["primary"]["remaining_percent"], 95)
        raw = json.dumps(data)
        record = tokens.read(self.path)
        for secret in (record["access_token"], record["refresh_token"]):
            self.assertNotIn(secret, raw)
        self.assertEqual(client.post("/chatgpt/api/accounts/other/refresh").status_code, 404)
        self.assertEqual(client.post("/chatgpt/api/accounts/default/logout").json()["state"], "logged_out")
        self.assertFalse(self.path.exists())
        denied = self.client(self.account(), allowed=False)
        for method, path in (("get", "/chatgpt/api/accounts"), ("post", "/chatgpt/api/accounts/default/login")):
            self.assertEqual(getattr(denied, method)(path).status_code, 403)
        self.assertEqual(denied.get("/chatgpt/ui", follow_redirects=False).status_code, 307)

    def test_watcher_runs_only_with_the_server(self):
        started = []

        class Probe(Account):
            async def run(self, interval=None):
                started.append(True)
                await asyncio.Event().wait()

        app = FastAPI()
        register(app, accounts=Accounts([Probe()]), admin_dependency=lambda: None)
        self.assertEqual(started, [])
        with TestClient(app):
            pass
        self.assertEqual(started, [True])


    # -- several subscriptions ---------------------------------------------

    def several(self, *names):
        return Accounts([self.account(name=name) for name in names])

    def test_accounts_come_from_the_environment(self):
        with patch.dict(os.environ, {"CHATGPT_ACCOUNTS": " default, team-2,Bad Name,team-2,,../x"}):
            with self.assertLogs("chatgpt_subscriptions", "ERROR"):
                names = [account.name for account in Accounts()]
        self.assertEqual(names, ["default", "team-2"])
        self.assertEqual([a.name for a in Accounts()], ["default"])
        self.assertEqual(Account("team-2").path, Path(self.tmp.name) / "accounts/team-2/auth.json")
        self.assertEqual(Account().path, self.path)
        for key, expected in ((None, None), ("sk-x", None), ("chatgpt-account:team-2", "team-2")):
            self.assertEqual(tokens.account_for_api_key(key), expected)
        with self.assertRaises(ValueError):
            tokens.auth_path("../x")

    def test_same_person_cannot_sign_in_to_two_subscriptions(self):
        self.store(expires_in=30 * 86400)
        accounts = self.several("default", "team-2")
        second = accounts.get("team-2")
        asyncio.run(self.settle(second))
        self.assertFalse(second.path.exists())
        self.assertEqual(second.status()["error"]["code"], accounts_module.DUPLICATE_ACCOUNT)
        self.assertIn("subscription default", second.status()["error"]["message"])
        self.assertIsNone(second.status()["known_account"])
        # Another member of the same workspace is a separate seat.
        self.fake.user, self.fake.email = "user-2", "colleague@example.com"

        async def finish():
            second.start_login()
            await asyncio.wait_for(second.login_task, 5)

        asyncio.run(finish())
        status = second.status()
        self.assertEqual((status["state"], status["email"]), ("ok", "colleague@example.com"))
        self.assertEqual(status["known_account"]["user_id"], "user-2")
        self.assertEqual(accounts.get("default").status()["email"], "team@example.com")

    def test_logout_forgets_the_person(self):
        self.store(expires_in=30 * 86400)
        accounts = self.several("default", "team-2")
        first, second = accounts.get("default"), accounts.get("team-2")

        async def run():
            await first.check()
            self.assertEqual(first.status()["known_account"]["user_id"], "user-1")
            tokens.write({"pending": True}, first.pending_path)
            first._update(pending_identity={"email": "other@example.com"})
            await first.logout()
            # The seat is free: the same person may now sign in to team-2 ...
            second.start_login()
            await asyncio.wait_for(second.login_task, 5)

        asyncio.run(run())
        status = first.status()
        self.assertEqual((status["known_account"], status["pending_account"]), (None, None))
        self.assertFalse(first.pending_path.exists())
        self.assertEqual((second.status()["state"], second.status()["known_account"]["user_id"]), ("ok", "user-1"))

        # ... and anyone signs in to the signed-out subscription without a confirmation.
        self.fake.user, self.fake.email = "user-2", "colleague@example.com"

        async def again():
            first.start_login()
            await asyncio.wait_for(first.login_task, 5)

        asyncio.run(again())
        self.assertEqual((first.status()["state"], first.status()["email"]), ("ok", "colleague@example.com"))

    def test_admin_api_and_prtg_per_subscription(self):
        self.store(expires_in=30 * 86400)
        accounts = self.several("default", "team-2")
        client = self.client(accounts, deployments=[
            {"model_name": "gpt-sub", "slug": "gpt-6-luna", "id": "dep-1", "account": "default", "credential": None, "problem": None},
            {"model_name": "gpt-sub", "slug": "gpt-6-luna", "id": "dep-2", "account": "team-2", "credential": None, "problem": None},
            {"model_name": "gpt-other-sub", "slug": "gpt-6-luna", "id": "dep-3", "account": "team-2", "credential": None, "problem": None},
        ])
        data = {a["account"]: a for a in client.get("/chatgpt/api/accounts").json()["accounts"]}
        self.assertEqual(list(data), ["default", "team-2"])
        self.assertEqual((data["default"]["state"], data["default"]["models"], data["default"]["requests_24h"]),
                         ("ok", ["gpt-sub"], 4))
        self.assertEqual((data["team-2"]["token_valid"], data["team-2"]["models"], data["team-2"]["requests_24h"]),
                         (False, ["gpt-other-sub", "gpt-sub"], 8))
        headers = {"X-Monitoring-Key": "monitor-secret"}
        sensor = lambda name: client.get(f"/chatgpt/accounts/{name}/usage/prtg?windows=primary", headers=headers)
        self.assertNotIn("error", sensor("default").json()["prtg"])
        self.assertEqual(sensor("team-2").json()["prtg"]["error"], 1)
        unknown = sensor("nobody")
        self.assertEqual((unknown.status_code, unknown.json()["prtg"]["error"]), (404, 1))
        self.assertEqual(client.get("/chatgpt/accounts/nobody/usage", headers=headers).status_code, 404)
        self.assertEqual(client.post("/chatgpt/api/accounts/team-2/logout").json()["account"], "team-2")
        self.assertTrue(self.path.exists())
        self.assertEqual(client.post("/chatgpt/api/accounts/nobody/login").status_code, 404)

    def test_broken_configuration_alarms_and_monitoring_overview(self):
        self.store(expires_in=30 * 86400)
        broken = {"model_name": "typo-sub", "slug": "gpt-typo", "id": "dep-9", "account": None,
                  "credential": "chatgpt-account-b", "problem": admin.CREDENTIAL_MISSING}
        good = {"model_name": "gpt-sub", "slug": "gpt-6-luna", "id": "dep-1", "account": "default",
                "credential": "chatgpt-default", "problem": None}
        client = self.client(self.account(), deployments=[good, broken])
        listed = client.get("/chatgpt/api/accounts").json()
        self.assertEqual([p["deployment"] for p in listed["problems"]], ["typo-sub"])
        self.assertEqual(listed["accounts"][0]["credentials"], ["chatgpt-default"])
        headers = {"X-Monitoring-Key": "monitor-secret"}
        sensor = client.get("/chatgpt/accounts/default/usage/prtg?windows=primary", headers=headers).json()["prtg"]
        self.assertEqual(sensor["error"], 1)
        self.assertIn("Configuration", sensor["text"])
        self.assertIn("typo-sub", sensor["text"])

        self.assertEqual(client.get("/chatgpt/accounts").status_code, 401)
        overview = client.get("/chatgpt/accounts", headers=headers)
        self.assertEqual(overview.headers["cache-control"], "no-store")
        data = overview.json()
        row = data["accounts"][0]
        self.assertEqual((row["account"], row["state"], row["credentials"], row["models"]),
                         ("default", "ok", ["chatgpt-default"], ["gpt-sub"]))
        self.assertEqual(row["quota"]["limits"][0]["primary"]["remaining_percent"], 95)
        self.assertEqual(data["problems"][0]["problem"], admin.CREDENTIAL_MISSING)
        raw = json.dumps(data)
        for private in ("team@example.com", "acct-1", "user-1"):
            self.assertNotIn(private, raw)

        healthy = self.client(self.account(), deployments=[good])
        sensor = healthy.get("/chatgpt/accounts/default/usage/prtg?windows=primary", headers=headers).json()["prtg"]
        self.assertNotIn("error", sensor)

    def test_watcher_runs_every_subscription(self):
        started = []

        class Probe(Account):
            async def run(self, interval=None):
                started.append(self.name)
                await asyncio.Event().wait()

        app = FastAPI()
        register(app, accounts=Accounts([Probe(), Probe("team-2")]), admin_dependency=lambda: None)
        with TestClient(app):
            pass
        self.assertEqual(sorted(started), ["default", "team-2"])


if __name__ == "__main__":
    unittest.main()
