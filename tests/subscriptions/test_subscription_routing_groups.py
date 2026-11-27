import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import litellm
import litellm.proxy.proxy_server as proxy_server
from litellm.proxy.extensions.chatgpt_subscriptions import access, counters, fair_use, tokens
from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Accounts
from litellm.proxy.extensions.chatgpt_subscriptions.expansion import (
    MEMBER,
    choose_managed,
    current_parent,
    expand_candidates,
)
from litellm.proxy.extensions.chatgpt_subscriptions.policy import retry_policy


class RoutingGroupTests(unittest.IsolatedAsyncioTestCase):
    """Managed subscription models reached through an alias or a routing group."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enter(patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": temp.name, "CHATGPT_ACCOUNTS": "a,b"}))
        self.accounts = Accounts()
        self.registry = self.accounts.registry
        self.ids = {a: r["subscription_id"] for a, r in self.registry.records().items()}
        self.enter(patch.object(access, "directory", AsyncMock(side_effect=lambda group, ids: {i: [] for i in ids})))
        for account in self.accounts:
            account.path.parent.mkdir(parents=True, exist_ok=True)
            tokens.write({"access_token": "synthetic", "expires_at": time.time() + 8000}, account.path)
        self.enter(patch.object(fair_use, "counters", counters.Counters()))
        self.enter(patch.object(counters, "proxy_db", return_value=None))
        self.enter(patch.dict(fair_use.quotas.snapshots, {}, clear=True))
        self.router = None
        self.make_router()
        self.row = SimpleNamespace(model_name="sol-sub", model_id="parent", blocked=False,
                                   litellm_params={"model": "chatgpt/gpt-5.4"}, model_info=self.assignment())
        prisma = Mock()
        prisma.db.litellm_proxymodeltable.find_unique = AsyncMock(side_effect=lambda where: self.row)
        prisma.db.litellm_proxymodeltable.find_many = AsyncMock(return_value=[])
        self.enter(patch.object(proxy_server, "prisma_client", prisma))
        self.data = {"metadata": {"user_api_key_user_id": "someone"}}

    def enter(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def assignment(self, mode="distribution"):
        return {"subscription_assignment": {"mode": mode, "revision": "v1",
                                            "subscription_ids": list(self.ids.values())}}

    def make_router(self, priorities=None):
        group = {"group_name": "sol", "models": ["sol-sub", "sol-azure"], "routing_strategy": "simple-shuffle"}
        if priorities:
            group |= {"routing_strategy": "priority", "model_priorities": priorities}
        router = litellm.Router(
            model_list=[
                {"model_name": "sol-azure", "litellm_params": {"model": "openai/gpt-4o", "api_key": "synthetic"},
                 "model_info": {"id": "azure"}},
                {"model_name": "sol-sub", "litellm_params": {"model": "chatgpt/gpt-5.4"},
                 "model_info": {"id": "parent", "db_model": True, **self.assignment()}},
            ],
            model_group_alias={"sol-sub2": "sol-sub"},
            routing_groups=[group],
        )
        self.addCleanup(router.discard)
        self.router = router
        self.enter(patch.object(proxy_server, "llm_router", router))

    def group(self, priorities=None):
        self.make_router(priorities)

    def copy(self, name, order=None):
        params = {"model": "chatgpt/gpt-5.4"} | ({"order": order} if order is not None else {})
        return {"model_name": name, "litellm_params": params,
                "model_info": {"id": "parent", "db_model": True, **self.assignment()}}

    def azure(self, order=None):
        params = {"model": "openai/gpt-4o", "api_key": "synthetic"} | ({"order": order} if order is not None else {})
        return {"model_name": "sol", "litellm_params": params, "model_info": {"id": "azure"}}

    async def test_parent_reached_through_alias_or_group_stays_current(self):
        self.group()
        for name in ("sol-sub", "sol-sub2", "sol"):
            parent = await current_parent(self.copy(name, order=1))
            self.assertEqual(parent["model_name"], name)
        # A group's priority on the handed-out copy survives the refresh from the database.
        self.assertEqual((await current_parent(self.copy("sol", order=2)))["litellm_params"]["order"], 2)
        self.assertNotIn("order", (await current_parent(self.copy("sol-sub")))["litellm_params"])

    async def test_renamed_or_removed_parent_is_rejected(self):
        self.group()
        self.row.model_name = "renamed"
        for name in ("sol-sub", "sol-sub2", "sol"):
            with self.assertRaises(litellm.ServiceUnavailableError):
                await current_parent(self.copy(name))
        self.row = None
        with self.assertRaises(litellm.ServiceUnavailableError):
            await current_parent(self.copy("sol-sub"))

    async def test_group_expands_members_at_the_group_priority(self):
        self.group({"sol-sub": 1, "sol-azure": 2})
        data = {**self.data, "_target_order": 2}
        result = await expand_candidates(self.router, [self.copy("sol", 1), self.azure(2)], data)
        self.assertEqual([r["model_info"]["id"] for r in result][-1], "azure")
        members = [r for r in result if r["model_info"].get(MEMBER)]
        self.assertEqual(sorted(m["model_info"][MEMBER]["subscription_id"] for m in members), sorted(self.ids.values()))
        self.assertEqual({m["litellm_params"]["order"] for m in members}, {1})
        self.assertEqual({m["model_name"] for m in members}, {"sol"})
        # The router still needs the group's order fallback state.
        self.assertEqual(data["_target_order"], 2)

    async def test_group_falls_back_when_no_subscription_is_permitted(self):
        self.group({"sol-sub": 1, "sol-azure": 2})
        for alias in self.ids:
            self.registry.update_settings("access", {"users": ["other"]}, alias)
        result = await expand_candidates(self.router, [self.copy("sol", 1), self.azure(2)], self.data)
        self.assertEqual([r["model_info"]["id"] for r in result], ["azure"])

    async def test_duplicate_public_name_outside_a_group_is_rejected(self):
        with self.assertRaises(litellm.ServiceUnavailableError):
            await expand_candidates(self.router, [self.copy("sol-sub"), self.azure()], self.data)

    async def test_managed_choice_only_among_one_parents_members(self):
        self.group({"sol-sub": 1, "sol-azure": 2})
        result = await expand_candidates(self.router, [self.copy("sol", 1), self.azure(2)], self.data)
        self.assertIsNone(choose_managed(result, "sol"))
        members = [r for r in result if r["model_info"].get(MEMBER)]
        self.assertIn(choose_managed(members, "sol"), members)

    async def test_retry_policy_leaves_groups_to_native_routing(self):
        self.group({"sol-sub": 1, "sol-azure": 2})
        self.assertIsNone(await retry_policy(self.router, "sol", dict(self.data)))

    async def test_priority_group_candidates_from_the_router(self):
        self.group({"sol-sub": 1, "sol-azure": 2})
        _, candidates = self.router._common_checks_available_deployment(model="sol", request_kwargs={})
        result = await expand_candidates(self.router, candidates, dict(self.data))
        members = [r for r in result if r["model_info"].get(MEMBER)]
        self.assertEqual(len(members), len(self.ids))
        self.assertEqual({m["litellm_params"]["order"] for m in members}, {1})
        self.assertEqual([r["model_info"]["id"] for r in result if not r["model_info"].get(MEMBER)], ["azure"])
        self.assertEqual(litellm.utils.get_order_filtered_deployments(result, target_order=None), members)
        self.assertEqual([r["model_info"]["id"] for r in
                          litellm.utils.get_order_filtered_deployments(result, target_order=2)], ["azure"])
