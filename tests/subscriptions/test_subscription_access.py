import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from litellm.proxy.extensions.chatgpt_subscriptions import access
from litellm.proxy.extensions.chatgpt_subscriptions.access import Caller, Grants
from litellm.proxy.extensions.chatgpt_subscriptions.registry import Registry
from litellm.proxy.extensions.chatgpt_subscriptions.settings import Access, Factors, FairUse


class RegistryMigrationTests(unittest.TestCase):
    def test_v1_is_read_and_only_migrated_on_write(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": root}):
            registry = Registry()
            legacy = dict(subscription_id="stable", display_name="Seat", created_at=time.time(), retired=False)
            v1 = dict(version=1, accounts={"seat": legacy})
            registry.path.write_text(json.dumps(v1))
            self.assertEqual(registry.records()["seat"]["fair_use"], FairUse().model_dump())
            registry.seed(["seat"])
            self.assertEqual(json.loads(registry.path.read_text()), v1)
            factors = {"users": {"unknown": 0}, "teams": {"old": 2}}
            registry.update_settings("fair_use_factors", factors)
            registry.rename("seat", "Renamed")
            registry.create("Another")
            saved = json.loads(registry.path.read_text())
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["accounts"]["seat"]["subscription_id"], "stable")
            self.assertEqual(saved["accounts"]["seat"]["access"], Access().model_dump())
            self.assertEqual(saved["fair_use_factors"], factors)
            self.assertEqual(Registry().read(), saved)

    def test_settings_validation(self):
        for model, value in [(Factors, {"users": {"u": v}}) for v in (-1, 10.01, True, "2", float("nan"))] + [
            (Access, {"users": [" "]}), (FairUse, {"primary_threshold": 0}), (FairUse, {"secondary_threshold": 101}),
            (FairUse, {"primary_threshold": 10.0}), (FairUse, {"enabled": "yes"})]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                model.model_validate(value)


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_grants_memberships_and_explicit_zero_override(self):
        people = AsyncMock(return_value=[SimpleNamespace(user_id="member", teams=["a", "b", "deleted"]),
                                         SimpleNamespace(user_id="direct", teams=[])])
        teams = AsyncMock(return_value=[SimpleNamespace(team_id=t) for t in ("a", "b")])
        db = SimpleNamespace(db=SimpleNamespace(litellm_usertable=SimpleNamespace(find_many=people),
                                                 litellm_teamtable=SimpleNamespace(find_many=teams)))
        grants = {"users": ["direct", "gone"], "teams": ["a", "deleted"]}
        weights = {"teams": {"a": 2, "b": 4, "deleted": 10}, "users": {"direct": 0, "gone": 9}}
        user = Caller("member", None)
        with patch.object(access, "proxy_db", return_value=db), patch.object(access, "_directory", access.DualCache()):
            resolved = await Grants.resolve([{"access": grants}], weights, [user])
            await Grants.resolve([{"access": grants}], weights, [user])
        self.assertEqual((people.await_count, teams.await_count), (1, 1))
        self.assertTrue(resolved.permits(grants, user))
        self.assertTrue(resolved.permits(grants, Caller("direct", "b")))
        self.assertTrue(resolved.permits(grants, Caller(None, "a")))
        self.assertFalse(resolved.permits(grants, Caller("member", "b")))
        self.assertFalse(resolved.permits(grants, Caller("gone", "deleted")))
        self.assertTrue(resolved.permits(Access().model_dump(), Caller(None, None)))
        self.assertEqual(resolved.unknown(grants), {"users": ["gone"], "teams": ["deleted"]})
        self.assertEqual(resolved.factor(user), 4)
        self.assertEqual(resolved.factor(Caller("member", "a")), 2)
        self.assertEqual(resolved.factor(Caller("direct", "a")), 0)
        self.assertEqual(resolved.factor(Caller("gone", "deleted")), 1)

    def test_key_identity_never_contains_a_raw_token(self):
        caller = Caller.from_auth(SimpleNamespace(user_id=None, team_id=None, api_key="sk-synthetic"))
        self.assertRegex(caller.identity, r"^key:[a-f0-9]{64}$")
        digest = caller.identity[4:]
        self.assertEqual(Caller.from_request({"metadata": {"user_api_key_hash": digest}}).identity, caller.identity)
