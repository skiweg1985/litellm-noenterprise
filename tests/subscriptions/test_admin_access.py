"""Subscription administration uses the gateway's existing identity boundary."""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth import oidc_identity
from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Account, Accounts
from litellm.proxy.extensions.chatgpt_subscriptions.admin import litellm_admin


def request(method="GET", origin=None):
    headers = [(b"host", b"gateway.example.test")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": method, "scheme": "https", "path": "/chatgpt/api/accounts/default/login", "headers": headers, "query_string": b""})


class AdminBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def browser(self, role, method="GET", origin=None):
        user = SimpleNamespace(user_id="synthetic-admin", user_role=role, user_email="admin@example.test", organization_id=None, models=[])
        config = SimpleNamespace(origin="https://gateway.example.test")
        state = {"claims": {}, "session_hash": "synthetic-session"}
        with patch.object(oidc_identity, "current_session", AsyncMock(return_value=(config, user, state))), patch("litellm.proxy._experimental.mcp_server.oidc_policy.attach_user_permissions", AsyncMock()):
            auth = await oidc_identity.authenticate_browser(request(method, origin), oidc_identity.BROWSER_MARKER)
        return litellm_admin()(auth=auth)

    async def test_native_admin_key_and_browser_admin_can_manage_subscriptions(self):
        native = UserAPIKeyAuth(user_role="proxy_admin", api_key="sk-synthetic")
        self.assertIs(litellm_admin()(auth=native), native)
        self.assertEqual((await self.browser("proxy_admin")).oidc_identity_source, "browser")
        self.assertEqual((await self.browser("proxy_admin", "POST", "https://gateway.example.test")).user_role, "proxy_admin")

    async def test_read_only_and_personal_roles_cannot_read_subscription_identity(self):
        for role in ("proxy_admin_viewer", "internal_user", "internal_user_viewer"):
            with self.subTest(role=role), self.assertRaises(HTTPException) as error:
                await self.browser(role)
            self.assertEqual(error.exception.status_code, 403)

    async def test_admin_provider_token_does_not_gain_subscription_management(self):
        auth = UserAPIKeyAuth(user_role="proxy_admin", api_key="oidc-access:synthetic")
        auth.oidc_identity_source = "provider"
        with self.assertRaises(HTTPException) as error:
            litellm_admin()(auth=auth)
        self.assertEqual(error.exception.status_code, 403)

    async def test_oidc_browser_mutations_require_same_origin(self):
        for origin in (None, "https://other.example.test"):
            with self.subTest(origin=origin), self.assertRaises(HTTPException) as error:
                await self.browser("proxy_admin", "POST", origin)
            self.assertEqual(error.exception.status_code, 403)

    async def test_empty_default_configuration_never_starts_sign_in(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": directory}, clear=False):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CHATGPT_AUTO_LOGIN", None)
                account = Account()
                with patch.object(account, "start_login") as start:
                    await account.check()
                start.assert_not_called()
                self.assertFalse(account.status()["auto_login"])

    async def test_shutdown_waits_for_login_cleanup(self):
        cleaned = asyncio.Event()
        async def login():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        account = Account()
        account.login_task = asyncio.create_task(login())
        await asyncio.sleep(0)
        await Accounts([account]).stop_logins()
        self.assertTrue(cleaned.is_set())
        self.assertTrue(account.login_task.done())


if __name__ == "__main__":
    unittest.main()
