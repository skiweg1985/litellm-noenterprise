"""Request-time subscription refresh does not leak tokens or poison retries."""

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from litellm.llms.chatgpt import authenticator as auth


def token():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    return f"e30.{payload}.synthetic"


class AuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": self.temp.name, "CHATGPT_AUTH_FILE": "auth.json"})
        env.start()
        self.addCleanup(env.stop)
        self.instance = auth.Authenticator()
        self.path = Path(self.instance.auth_file)
        self.path.write_text(json.dumps({"access_token": "expired", "refresh_token": "synthetic-refresh", "expires_at": 1}))
        auth._REJECTED_REFRESH.clear()

    def test_first_legacy_dispatch_has_persisted_identity_before_watcher_runs(self):
        from litellm.proxy.extensions.chatgpt_subscriptions.registry import Registry
        record={'access_token':token(),'account_id':'synthetic-account','expires_at':time.time()+3600}
        self.path.write_text(json.dumps(record))
        access, provider, snapshot=self.instance.request_credentials()
        self.assertEqual(access,record['access_token'])
        self.assertEqual(provider,'synthetic-account')
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.connection_revision)
        self.assertEqual(snapshot.subscription_id,Registry().records()['default']['subscription_id'])
        self.assertEqual(snapshot.connection_revision,json.loads(self.path.read_text())['connection_revision'])
        _,_,again=self.instance.request_credentials()
        self.assertEqual(snapshot,again)

    def test_transient_transport_failure_does_not_poison_retry(self):
        calls = []
        new_token = token()
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("sensitive-provider-detail", request=request)
            return httpx.Response(200, json={"access_token": new_token, "id_token": new_token, "refresh_token": "rotated-synthetic"})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, patch.object(auth, "get_httpx_client", return_value=client):
            with self.assertRaises(auth.GetAccessTokenError):
                self.instance.get_access_token()
            self.assertEqual(self.instance.get_access_token(), new_token)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "rotated-synthetic")

    def test_rejected_refresh_is_not_reused(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(401, json={"error": "sensitive-provider-detail"})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, patch.object(auth, "get_httpx_client", return_value=client):
            for _ in range(2):
                with self.assertRaises(auth.GetAccessTokenError) as error:
                    self.instance.get_access_token()
                self.assertNotIn("sensitive-provider-detail", str(error.exception))
        self.assertEqual(len(calls), 1)

    def test_incomplete_refresh_response_never_exposes_tokens(self):
        def handle(request):
            return httpx.Response(200, json={"access_token": "sensitive-provider-detail"})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, patch.object(auth, "get_httpx_client", return_value=client):
            with self.assertRaises(auth.RefreshAccessTokenError) as error:
                self.instance._refresh_tokens("synthetic-refresh")
        self.assertEqual(error.exception.status_code, 502)
        self.assertNotIn("sensitive-provider-detail", str(error.exception))

    def test_metadata_update_cannot_restore_deleted_credentials(self):
        seen = json.loads(self.path.read_text())
        self.path.unlink()
        self.instance._merge_auth_file(seen, {"account_id": "synthetic-account"})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
