"""Public subscription HTTP lifecycle with persistent, synthetic provider state."""
import os
import tempfile
import unittest
import time
import httpx
from test_chatgpt_login import FakeAuth
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Accounts
from litellm.proxy.extensions.chatgpt_subscriptions.routes import register

class RegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": self.directory.name, "CHATGPT_ACCOUNTS": "default", "CHATGPT_AUTO_LOGIN": "false", "CHATGPT_DEVICE_POLL_MIN_SECONDS": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def client(self, transport=None):
        app = FastAPI()
        register(app, accounts=Accounts(transport=transport), admin_dependency=lambda: None, watch=False)
        return TestClient(app)

    def test_create_rename_and_restart_preserve_identity_and_alias(self):
        first, second = self.client(), self.client()
        created = first.post('/chatgpt/api/accounts', json={'display_name': 'Development'})
        self.assertEqual(created.status_code, 200, created.text)
        account = created.json()
        self.assertEqual(account['display_name'], 'Development')
        self.assertFalse(account['token_valid'])
        self.assertIsNone(account['connection_revision'])
        renamed = second.patch('/chatgpt/api/accounts/' + account['account'], json={'display_name': 'Research'})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()['subscription_id'], account['subscription_id'])
        restored = next(a for a in self.client().get('/chatgpt/api/accounts').json()['accounts'] if a['subscription_id'] == account['subscription_id'])
        self.assertEqual(restored['display_name'], 'Research')
        self.assertEqual(restored['account'], account['account'])

    def test_retired_account_is_a_tombstone_and_cannot_reconnect_or_rename(self):
        client = self.client()
        account = client.post('/chatgpt/api/accounts', json={'display_name': 'Retiring'}).json()
        prefix = '/chatgpt/api/accounts/' + account['account']
        retired = client.post(prefix + '/retire')
        self.assertEqual(retired.status_code, 200, retired.text)
        self.assertTrue(retired.json()['retired'])
        self.assertFalse(retired.json()['token_valid'])
        self.assertEqual(retired.json()['subscription_id'], account['subscription_id'])
        self.assertEqual(client.post(prefix + '/login').status_code, 409)
        self.assertEqual(client.patch(prefix, json={'display_name': 'Resurrected'}).status_code, 409)
        restored = next(a for a in self.client().get('/chatgpt/api/accounts').json()['accounts'] if a['account'] == account['account'])
        self.assertEqual(restored['state'], 'retired')

    def test_invalid_names_and_denied_mutations_do_not_create_accounts(self):
        client = self.client()
        for name in ('', ' ', 'x' * 101, 'bad\nname'):
            self.assertIn(client.post('/chatgpt/api/accounts', json={'display_name': name}).status_code, (400, 422))
        self.assertEqual(len(client.get('/chatgpt/api/accounts').json()['accounts']), 1)

    def wait_account(self, client, alias, predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            account = next(a for a in client.get('/chatgpt/api/accounts').json()['accounts'] if a['account'] == alias)
            if predicate(account):
                return account
            time.sleep(0.05)
        self.fail('Expected account state was not reached: ' + str(account['state']))

    def test_connected_account_revision_survives_renewal_and_changes_only_on_replacement(self):
        provider = FakeAuth()
        with self.client(httpx.MockTransport(provider)) as client:
            account = client.post('/chatgpt/api/accounts', json={'display_name': 'Development'}).json()
            alias = account['account']
            endpoint = '/chatgpt/api/accounts/' + alias
            self.assertEqual(client.post(endpoint + '/login').status_code, 200)
            connected = self.wait_account(client, alias, lambda a: a['token_valid'])
            revision = connected['connection_revision']
            self.assertIsInstance(revision, str)
            refreshed = client.post(endpoint + '/refresh').json()
            self.assertEqual(refreshed['connection_revision'], revision)
            self.assertTrue(refreshed['token_valid'])
            provider.user, provider.email = 'second-person', 'second@example.test'
            client.post(endpoint + '/login')
            pending = self.wait_account(client, alias, lambda a: a['pending_account'])
            self.assertEqual(pending['connection_revision'], revision)
            replaced = client.post(endpoint + '/pending/confirm').json()
            self.assertNotEqual(replaced['connection_revision'], revision)
            self.assertEqual(replaced['subscription_id'], connected['subscription_id'])
            disconnected = client.post(endpoint + '/logout').json()
            self.assertEqual(disconnected['state'], 'logged_out')
            self.assertEqual(disconnected['connection_revision'], replaced['connection_revision'])

    def test_duplicate_provider_identity_is_rejected_for_a_second_dynamic_account(self):
        provider = FakeAuth()
        with self.client(httpx.MockTransport(provider)) as client:
            first = client.post('/chatgpt/api/accounts', json={'display_name': 'First'}).json()['account']
            second = client.post('/chatgpt/api/accounts', json={'display_name': 'Second'}).json()['account']
            client.post('/chatgpt/api/accounts/' + first + '/login')
            self.wait_account(client, first, lambda a: a['token_valid'])
            client.post('/chatgpt/api/accounts/' + second + '/login')
            rejected = self.wait_account(client, second, lambda a: a['error'])
            self.assertEqual(rejected['error']['code'], 'duplicate_account')
            self.assertFalse(rejected['token_valid'])
            self.assertIsNone(rejected['connection_revision'])

    def test_retry_after_duplicate_during_provider_cleanup_is_not_lost(self):
        self.retry_during_provider_cleanup()

    def test_other_worker_preserves_retry_after_duplicate_during_provider_cleanup(self):
        self.retry_during_provider_cleanup(other_worker=True)

    def test_cancel_fences_retry_queued_during_provider_cleanup(self):
        self.retry_during_provider_cleanup(cancel=True)

    def test_other_worker_cancel_fences_retry_queued_during_provider_cleanup(self):
        self.retry_during_provider_cleanup(cancel=True, remote_cancel=True)

    def retry_during_provider_cleanup(self, other_worker=False, cancel=False, remote_cancel=False):
        import asyncio
        import threading

        provider = FakeAuth()
        closing, release = threading.Event(), threading.Event()

        class ProviderTransport(httpx.MockTransport):
            """Keep the rejected provider connection closing until the retry arrives."""
            gate_cleanup = False
            exchange_task = None

            async def handle_async_request(self, request):
                if request.url.path == '/oauth/token':
                    self.exchange_task = asyncio.current_task()
                return await super().handle_async_request(request)

            async def aclose(self):
                if self.gate_cleanup and asyncio.current_task() is self.exchange_task:
                    closing.set()
                    while not release.is_set():
                        await asyncio.sleep(0.01)
                await super().aclose()

        transport = ProviderTransport(provider)
        with self.client(transport) as client, self.client(httpx.MockTransport(provider)) as worker:
            first = client.post('/chatgpt/api/accounts', json={'display_name': 'First'}).json()['account']
            second = client.post('/chatgpt/api/accounts', json={'display_name': 'Second'}).json()['account']
            client.post('/chatgpt/api/accounts/' + first + '/login')
            self.wait_account(client, first, lambda a: a['token_valid'] and a['login'] is None)
            transport.gate_cleanup = True
            endpoint = '/chatgpt/api/accounts/' + second + '/login'
            client.post(endpoint)
            try:
                self.assertTrue(closing.wait(6), 'Provider connection did not reach cleanup')
                rejected = self.wait_account(client, second, lambda a: a['error'])
                self.assertEqual(rejected['error']['code'], 'duplicate_account')
                provider.user, provider.email = 'second-person', 'second@example.test'
                retry_client = worker if other_worker else client
                self.assertEqual(retry_client.post(endpoint).status_code, 200)
                if cancel:
                    cancelling_client = worker if remote_cancel else client
                    self.assertEqual(cancelling_client.post(endpoint + '/cancel').status_code, 200)
            finally:
                release.set()
            if cancel:
                cancelled = self.wait_account(client, second, lambda a: a['state'] == 'logged_out')
                self.assertFalse(cancelled['token_valid'])
                self.assertIsNone(cancelled['login'])
                self.assertEqual(provider.calls.count('/api/accounts/deviceauth/usercode'), 2)
                return
            connected = self.wait_account(client, second, lambda a: a['token_valid'])
            self.assertEqual(connected['email'], 'second@example.test')
            self.assertIsNone(connected['error'])

    def test_nondefault_legacy_accounts_share_the_request_registry(self):
        from pathlib import Path
        from litellm.llms.chatgpt.authenticator import Authenticator
        for index, names in enumerate(('account-a', 'account-a,account-b')):
            with self.subTest(names=names), patch.dict(os.environ, {'CHATGPT_ACCOUNTS': names, 'CHATGPT_TOKEN_DIR': str(Path(self.directory.name) / str(index))}):
                client = self.client()
                created = client.post('/chatgpt/api/accounts', json={'display_name': 'Added'}).json()
                selected = Authenticator.for_request(api_key='chatgpt-account:' + created['account'])
                self.assertEqual(selected.account, created['account'])
                aliases = {a['account'] for a in self.client().get('/chatgpt/api/accounts').json()['accounts']}
                self.assertEqual(aliases, set(names.split(',')) | {created['account']})

    def test_retirement_from_another_worker_fences_a_late_token_exchange(self):
        import asyncio
        import threading
        entered, release = threading.Event(), threading.Event()
        provider = FakeAuth()
        async def delayed(request):
            if request.url.path == '/oauth/token':
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            return provider(request)
        with self.client(httpx.MockTransport(delayed)) as first, self.client() as second:
            created = first.post('/chatgpt/api/accounts', json={'display_name': 'Late exchange'}).json()
            endpoint = '/chatgpt/api/accounts/' + created['account']
            first.post(endpoint + '/login')
            self.assertTrue(entered.wait(timeout=6))
            retired = second.post(endpoint + '/retire')
            self.assertEqual(retired.status_code, 200, retired.text)
            release.set()
            time.sleep(0.1)
            result = self.wait_account(first, created['account'], lambda a: a['state'] == 'retired')
            self.assertFalse(result['token_valid'])
            self.assertIsNone(result['login'])
            self.assertIsNone(result['connection_revision'])
