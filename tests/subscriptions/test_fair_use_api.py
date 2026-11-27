import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.extensions.chatgpt_subscriptions import access, enforcement, fair_use, fair_use_api
from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Accounts


class FairUseApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": self.tmp.name, "CHATGPT_ACCOUNTS": "first,second"})
        env.start()
        self.addCleanup(env.stop)
        self.accounts = Accounts()
        self.records = self.accounts.registry.records()
        self.app = FastAPI()
        self.app.include_router(fair_use_api.router(self.accounts))
        self.auth = UserAPIKeyAuth(user_id="mine", user_role="proxy_admin")
        self.app.dependency_overrides[user_api_key_auth] = lambda: self.auth
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        directory = patch.object(access, "directory", AsyncMock(side_effect=lambda group, ids: {
            id_: [] for id_ in ids if id_ not in ("deleted", "missing")}))
        directory.start()
        self.addCleanup(directory.stop)
        self.first = "/chatgpt/api/accounts/first/"

    def test_contract_retirement_and_audit(self):
        with patch.object(fair_use_api, "audit", AsyncMock()) as audit:
            for path, expected in [
                (self.first + "access", {"users": ["mine", "deleted"], "teams": []}),
                (self.first + "fair-use", {"enabled": False, "primary_threshold": 75, "secondary_threshold": 90}),
                ("/chatgpt/api/fair-use/factors", {"users": {}, "teams": {"missing": 2.5}}),
            ]:
                written = self.client.put(path, json=expected)
                self.assertEqual(written.status_code, 200, written.text)
                self.assertEqual(written.headers["Cache-Control"], "no-store")
                self.assertEqual(written.json(), expected)
                self.assertEqual(self.client.get(path).json(), expected)
            self.assertEqual(audit.await_count, 3)
            status = self.client.get("/chatgpt/api/fair-use/status").json()
            row = next(a for a in status["accounts"] if a["account"] == "first")
            self.assertEqual(row["unknown_ids"], {"teams": ["missing"], "users": ["deleted"]})
            self.assertEqual(row["quota_state"], "unavailable")
            self.assertEqual(self.client.put(self.first + "fair-use", json={"primary_threshold": 0}).status_code, 422)
            asyncio.run(self.accounts.retire("first"))
            self.assertEqual(self.client.put(self.first + "access", json={}).status_code, 409)
            self.assertEqual(self.client.get(self.first + "access").status_code, 200)
            self.assertEqual(audit.await_count, 3)

    def test_model_weights_inventory_replacement_and_audit(self):
        path = "/chatgpt/api/fair-use/model-weights"
        overrides = {"future": {"like": "gpt-6-astra"}, "seen": {"input": 1, "output": 2}}
        deployments = [
            {"model_name": "parent", "model_info": {"subscription_assignment": {"mode": "fixed"}},
             "litellm_params": {"model": "chatgpt/gpt-6-luna"}},
            {"model_name": "configured", "litellm_params": {"model": "chatgpt/gpt-6-sol"}},
            {"model_name": "unrelated", "litellm_params": {"model": "openai/gpt-4o"}},
        ]
        server = SimpleNamespace(llm_router=SimpleNamespace(model_list=deployments))
        now = time.time()
        window = {"reset_at": now + 60, "window_seconds": 3600, "used_percent": 60}
        snapshot = {"fetched_at": now - 3600, "primary": {**window, "reset_at": now - 10}, "secondary": window}
        counts = {"u": {"models": {"seen": [1, 0, 0], "counter-only": [0, 0, 0], None: [2, 0, 0]}}}
        with patch.dict(sys.modules, {"litellm.proxy.proxy_server": server}), \
             patch.dict(fair_use.quotas.snapshots, {"seat": snapshot}, clear=True), \
             patch.object(fair_use_api.counters, "counts", AsyncMock(return_value=counts)) as read_counts, \
             patch.object(fair_use_api, "audit", AsyncMock()) as audit:
            written = self.client.put(path, json=overrides)
            self.assertEqual(written.status_code, 200, written.text)
            self.assertEqual(written.headers["cache-control"], "no-store")
            read_counts.assert_awaited_once_with("seat", "secondary", window)
            audit.assert_awaited_once_with("model_weights", "model_weights", {}, overrides, self.auth)
            fetched = self.client.get(path)
            self.assertEqual(fetched.headers["cache-control"], "no-store")
            self.assertEqual(fetched.json(), written.json())
            body = fetched.json()
            self.assertEqual(set(body), {"reference_model", "reference_fallback", "overrides", "models"})
            self.assertEqual(body["overrides"], overrides)
            self.assertEqual(body["reference_model"], "gpt-6-sol")
            self.assertFalse(body["reference_fallback"])
            self.assertEqual([r["model"] for r in body["models"]],
                             ["counter-only", "future", "gpt-6-luna", "gpt-6-sol", "seen"])
            future = body["models"][1]
            self.assertEqual(set(future), {"model", "input", "output", "cache_read", "source", "weight", "unpriced"})
            self.assertEqual(future["source"], "like")
            self.assertAlmostEqual(future["weight"], 5)
            invalid = self.client.put(path, json={"one": {"like": "two"}, "two": {"like": "three"}})
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(invalid.headers["cache-control"], "no-store")
            self.assertEqual(self.client.get(path).json(), body)
            self.assertEqual(self.client.put(path, json={}).json()["overrides"], {})
            self.assertEqual(audit.await_count, 2)

    def test_enabled_audit_writes_settings_to_the_native_table(self):
        import litellm
        create = AsyncMock()
        db = SimpleNamespace(db=SimpleNamespace(litellm_auditlog=SimpleNamespace(create=create)))
        server = SimpleNamespace(premium_user=False, prisma_client=db)
        with patch.dict(sys.modules, {"litellm.proxy.proxy_server": server}), patch.object(litellm, "store_audit_logs", True):
            response = self.client.put(self.first + "access", json={"users": ["mine"], "teams": []})
            self.assertEqual(response.status_code, 200, response.text)
        data = create.await_args.kwargs["data"]
        self.assertEqual(data["table_name"], "ChatGPT_SubscriptionRegistry")
        self.assertEqual(data["changed_by"], "mine")
        self.assertEqual(json.loads(data["before_value"])["access"]["users"], [])
        self.assertEqual(json.loads(data["updated_values"])["access"]["users"], ["mine"])

    def test_personal_get_admitted_by_browser_and_provider_route_checks(self):
        from starlette.requests import Request
        from litellm.proxy.auth import oidc_api, oidc_identity
        from litellm.proxy.auth.route_checks import RouteChecks
        path = "/chatgpt/fair-use/me"
        request = Request({"type": "http", "path": path, "method": "GET", "headers": [], "query_string": b""})
        self.assertTrue(RouteChecks.is_info_route(path))
        for check in (oidc_identity.authorize_browser_route, oidc_api.authorize_api_route):
            check(request, "internal_user_viewer")

    def test_non_admin_only_sees_own_shares_and_permitted_accounts(self):
        self.auth.user_role = "internal_user"
        for path in [self.first + "access", self.first + "fair-use", "/chatgpt/api/fair-use/factors",
                     "/chatgpt/api/fair-use/model-weights", "/chatgpt/api/fair-use/status"]:
            denied = self.client.get(path)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.headers["cache-control"], "no-store")
            if not path.endswith("status"):
                denied = self.client.put(path, json={})
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(denied.headers["cache-control"], "no-store")
        self.accounts.registry.update_settings("access", {"users": ["other-private-user"]}, "second")
        now = time.time()
        q = {"reset_at": now + 300, "window_seconds": 18000, "used_percent": 70}
        snapshot = dict(fetched_at=now, primary=q, secondary=q)
        users = {"mine": {"models": {"gpt-6-sol": [2, 1, 1]}, "team": None}, "other-private-user": {"models": {"gpt-6-sol": [80, 0, 0]}, "team": None}}
        with patch.dict(fair_use.quotas.snapshots, {self.records["first"]["subscription_id"]: snapshot}), \
             patch.object(fair_use.counters, "counts", AsyncMock(return_value=users)):
            response = self.client.get("/chatgpt/fair-use/me")
            self.auth.user_role = "proxy_admin"
            status = self.client.get("/chatgpt/api/fair-use/status")
            first = next(row for row in status.json()["accounts"] if row["account"] == "first")
            for window in first["windows"]:
                mine = next(user for user in window["users"] if user["user_id"] == "mine")
                self.assertAlmostEqual(mine["consumption"], 2 * 2e-6 + 2e-7 + 1e-5)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["user_id"], "mine")
        self.assertEqual([r["display_name"] for r in body["accounts"]], ["first"])
        self.assertNotIn("other-private-user", response.text)
        self.assertNotIn("subscription_id", response.text)
        for w in body["accounts"][0]["windows"]:
            self.assertEqual(set(w), {"id", "scarce", "threshold", "used_percent", "reset_at", "tokens",
                                      "consumption", "factor", "share", "allowance", "throttled"})
            self.assertEqual(w["tokens"], 4)
            consumption = 2 * 2e-6 + 2e-7 + 1e-5
            self.assertAlmostEqual(w["consumption"], consumption)
            self.assertAlmostEqual(w["share"], consumption / (80 * 2e-6 + consumption) * 70)

    def test_listing_respects_grants_and_admin_management_view(self):
        self.accounts.registry.update_settings("access", {"teams": ["restricted-team"]}, "second")
        first, second = (self.records[a]["subscription_id"] for a in ("first", "second"))
        def managed(name, ids):
            return {"model_name": name, "model_info": {"subscription_assignment": {"subscription_ids": ids}},
                    "litellm_params": {"model": "chatgpt/test"}}
        def config(name, alias):
            return {"model_name": name, "litellm_params": {"model": "chatgpt/test", "api_key": "chatgpt-account:" + alias}}
        rows = [managed("fixed", [second]), managed("reserve", [second, first]), config("config", "second"),
                config("mixed", "first"), config("mixed", "second")]
        router = SimpleNamespace(model_list=rows, get_model_list=lambda: rows)
        for role in ("proxy_admin", "internal_user"):
            self.auth.user_role = role
            self.assertEqual(asyncio.run(enforcement.hidden_models(router, self.auth)), {"fixed", "config"})
        self.auth.user_role = "proxy_admin"
        self.assertEqual(asyncio.run(enforcement.visible_deployments(rows, self.auth, management=True)), rows)
