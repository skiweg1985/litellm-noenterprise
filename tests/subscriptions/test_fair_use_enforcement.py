import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import litellm
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.extensions.chatgpt_subscriptions import access, counters, enforcement, fair_use
from litellm.proxy.extensions.chatgpt_subscriptions.accounts import Accounts
from litellm.proxy.extensions.chatgpt_subscriptions.attribution import CredentialSnapshot, begin
from litellm.proxy.extensions.chatgpt_subscriptions.expansion import expand_candidates, MEMBER
from litellm.proxy.extensions.chatgpt_subscriptions.fair_use_math import utc
from litellm.proxy.extensions.chatgpt_subscriptions.policy import record_failure
from litellm.router_utils.cooldown_handlers import set_cooldown_deployments


class EnforcementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enter(patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": temp.name, "CHATGPT_ACCOUNTS": "a,b,c"}))
        self.accounts = Accounts()
        self.registry = self.accounts.registry
        self.ids = {a: r["subscription_id"] for a, r in self.registry.records().items()}
        self.enter(patch.object(access, "directory", AsyncMock(side_effect=lambda group, ids: {i: [] for i in ids})))
        for account in self.accounts:
            account.path.parent.mkdir(parents=True, exist_ok=True)
            from litellm.proxy.extensions.chatgpt_subscriptions import tokens
            tokens.write({"access_token": "synthetic", "expires_at": time.time() + 8000}, account.path)
        self.store = counters.Counters()
        self.enter(patch.object(fair_use, "counters", self.store))
        self.enter(patch.object(counters, "counters", self.store))
        self.enter(patch.object(counters, "proxy_db", return_value=None))
        self.enter(patch.dict(fair_use.quotas.snapshots, {}, clear=True))
        self.router = Mock(spec=litellm.Router)
        self.router.cooldown_cache = Mock()
        self.router.cooldown_cache.async_get_active_cooldowns = AsyncMock(return_value=[])
        self.data = {"metadata": {"user_api_key_user_id": "heavy"}}
        self.now = time.time()

    def enter(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def parent(self, mode):
        return {"model_name": "public", "litellm_params": {"model": "chatgpt/test"}, "model_info": {
            "id": "parent", "subscription_assignment": {"mode": mode, "revision": "v1",
                                                          "subscription_ids": list(self.ids.values())}}}

    def quota(self, alias, primary, secondary, delay=300):
        fair_use.quotas.snapshots[self.ids[alias]] = {
            "fetched_at": self.now,
            "primary": dict(used_percent=primary, window_seconds=18000, reset_at=self.now + delay),
            "secondary": dict(used_percent=secondary, window_seconds=604800, reset_at=self.now + 2 * delay),
        }
        for user, count in (("heavy", 90), ("light", 10)):
            self.store.record(self.ids[alias], [user, self.now, user, None, "test", [count, 0, 0], True])

    async def test_modes_order_equal_distribution_and_denial_precedence(self):
        self.registry.update_settings("access", {"users": ["other"]}, "b")
        for mode, orders in [("priority", [1, 3]), ("distribution", [1, 1])]:
            result = await expand_candidates(self.router, [self.parent(mode)], self.data)
            self.assertEqual([r["model_info"][MEMBER]["subscription_id"] for r in result], [self.ids["a"], self.ids["c"]])
            self.assertEqual([r["litellm_params"]["order"] for r in result], orders)
        self.quota("a", 95, 0)
        self.quota("c", 0, 90, 45)
        with self.assertRaises(enforcement.FairUseLimitExceeded) as caught:
            await expand_candidates(self.router, [self.parent("priority")], self.data)
        self.assertIn("weekly", caught.exception.message)
        self.assertIn(utc(self.now + 90), caught.exception.message)
        self.assertLessEqual(int(caught.exception.headers["Retry-After"]), 90)
        for alias in ["a", "c"]:
            self.registry.update_settings("access", {"users": ["other"]}, alias)
        with self.assertRaises(enforcement.SubscriptionAccessDenied):
            await expand_candidates(self.router, [self.parent("distribution")], self.data)

    async def test_plan_with_only_a_weekly_primary_window(self):
        for alias in ["b", "c"]:
            self.registry.update_settings("access", {"users": ["other"]}, alias)
        fair_use.quotas.snapshots[self.ids["a"]] = {
            "fetched_at": self.now, "secondary": None,
            "primary": dict(used_percent=70, window_seconds=604800, reset_at=self.now + 300),
        }
        for user, count in (("heavy", 90), ("light", 10)):
            self.store.record(self.ids["a"], [user, self.now, user, None, "test", [count, 0, 0], True])
        with self.assertRaises(enforcement.FairUseLimitExceeded) as caught:
            await expand_candidates(self.router, [self.parent("priority")], self.data)
        self.assertIn("The weekly fair-use limit", caught.exception.message)
        light = {"metadata": {"user_api_key_user_id": "light"}}
        self.assertEqual(len(await expand_candidates(self.router, [self.parent("priority")], light)), 1)
        logger = SimpleNamespace(model_call_details={"litellm_params": light})
        await enforcement.dispatch(logger, CredentialSnapshot(self.ids["a"], "A", None))
        self.assertEqual(fair_use.dispatched_headers(logger)["x-litellm-fair-use-allowance"], "50.0")

    async def test_config_precheck_candidates_and_actual_dispatch(self):
        from litellm.proxy.route_llm_request import _raise_if_model_fully_blocked

        self.quota("a", 90, 5)
        self.registry.update_settings("access", {"users": ["other"]}, "b")
        direct = {"model_name": "public", "litellm_params": {"model": "chatgpt/test", "api_key": "chatgpt-account:a"}}
        named = {"model_name": "public", "litellm_params": {"model": "chatgpt/test", "litellm_credential_name": "cred"}}
        third = {"model_name": "public", "litellm_params": {"model": "chatgpt/test", "api_key": "chatgpt-account:c"}}
        self.enter(patch.object(enforcement, "_credential_values", return_value={"api_key": "chatgpt-account:b"}))
        self.router.are_all_deployments_blocked.return_value = False
        self.router.get_model_list.return_value = [direct, named]
        with self.assertRaises(enforcement.FairUseLimitExceeded):
            await _raise_if_model_fully_blocked(self.router, "public", None, self.data)
        self.router.get_model_list.return_value.append(third)
        await _raise_if_model_fully_blocked(self.router, "public", None, self.data)
        self.assertEqual(await expand_candidates(self.router, [direct, named, third], self.data), [third])
        logger = SimpleNamespace(model_call_details={"litellm_params": self.data})
        with self.assertRaises(enforcement.SubscriptionAccessDenied):
            await enforcement.dispatch(logger, CredentialSnapshot(self.ids["b"], "B", None))

    async def test_router_completion_stops_before_provider_or_backup(self):
        self.quota("a", 90, 0)
        router = litellm.Router(model_list=[
            {"model_name": "public", "litellm_params": {"model": "chatgpt/gpt-5.4", "api_key": "chatgpt-account:a"}},
            {"model_name": "backup", "litellm_params": {"model": "openai/gpt-4o", "api_key": "synthetic"}},
        ], fallbacks=[{"public": ["backup"]}], num_retries=4)
        self.addCleanup(router.discard)
        with patch.object(litellm, "acompletion", AsyncMock()) as provider:
            with self.assertRaises(enforcement.FairUseLimitExceeded):
                await router.acompletion(model="public", messages=[{"role": "user", "content": "test"}], **self.data)
            provider.assert_not_awaited()
        self.assertFalse(any(router.fail_calls.values()))

    async def test_dispatched_headers_and_count_on_finish_only_once(self):
        from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing

        self.quota("a", 90, 5)
        logger = SimpleNamespace(model_call_details={"litellm_params": {"litellm_metadata": {"user_api_key_user_id": "light"}}})
        credential = CredentialSnapshot(self.ids["a"], "A", None)
        await enforcement.dispatch(logger, credential)
        headers = ProxyBaseLLMRequestProcessing.get_custom_headers(user_api_key_dict=UserAPIKeyAuth(),
                                                                   litellm_logging_obj=logger)
        self.assertEqual(headers["x-litellm-fair-use-reset"], utc(self.now + 300))
        self.assertEqual(headers["x-litellm-fair-use-share"], "9.0")
        self.assertEqual(headers["x-litellm-fair-use-allowance"], "50.0")
        attempt = begin(logger, credential, "test")
        attempt.finish("success", {"usage": {"input_tokens": 7, "output_tokens": 3}})
        attempt.finish("failure")
        window = fair_use.quotas.snapshots[self.ids["a"]]["primary"]
        self.assertEqual((await self.store.counts(self.ids["a"], "primary", window))["light"]["models"], {"test": [17, 0, 3]})
        self.registry.update_settings("fair_use", {"enabled": False}, "a")
        await enforcement.dispatch(logger, credential)
        self.assertEqual(logger._subscription_fair_use_headers, {})
        self.registry.update_settings("fair_use", {"enabled": True}, "a")
        fair_use.quotas.snapshots[self.ids["a"]]["fetched_at"] -= 3600
        await enforcement.dispatch(logger, credential)
        self.assertEqual(logger._subscription_fair_use_headers, {})

    async def test_override_reprices_existing_window(self):
        self.quota("a", 90, 0)
        self.store.record(self.ids["a"], ["expensive", self.now, "light", None, "future", [5, 0, 0], True])

        async def shares():
            settings = self.registry.read()
            _, windows = await fair_use.evaluate(settings["accounts"]["a"], settings["fair_use_factors"],
                                                  settings["model_weights"])
            return windows[0]["users"]

        before = await shares()
        self.registry.update_settings("model_weights", {"future": {"factor": 100}})
        after = await shares()
        self.assertTrue(before["heavy"]["throttled"])
        self.assertTrue(after["light"]["throttled"])
        self.assertFalse(after["heavy"]["throttled"])
        self.assertEqual(before["light"]["tokens"], after["light"]["tokens"])
        self.assertGreater(after["light"]["consumption"], before["light"]["consumption"])

    def test_cached_input_attribution(self):
        from litellm.types.llms.openai import ResponseAPIUsage

        logger = SimpleNamespace(model_call_details={})
        for usage, count in [
            ({"input_tokens": 8, "input_tokens_details": {"cached_tokens": 3}}, 3),
            ({"prompt_tokens": 8, "prompt_tokens_details": {"cached_tokens": 0}}, 0),
            ({"input_tokens": 8}, None),
            (ResponseAPIUsage(input_tokens=8, output_tokens=2, total_tokens=10,
                              input_tokens_details={"text_tokens": 8}), None),
        ]:
            with self.subTest(usage=usage):
                attempt = begin(logger, CredentialSnapshot(self.ids["a"], "A", None), "model")
                attempt.finish("success", {"usage": usage})
                self.assertEqual(attempt.value["cached_input_tokens"], count)

    async def test_openai_error_mapping_and_router_skips_health_and_retries(self):
        from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing

        router = litellm.Router(model_list=[{"model_name": "test", "litellm_params": {"model": "openai/gpt-4o", "api_key": "synthetic"}}],
                                num_retries=4, fallbacks=[{"test": ["backup"]}])
        self.addCleanup(router.discard)
        rate = enforcement.FairUseLimitExceeded([(dict(id="primary", name="5-hour", window_seconds=18000, reset_at=self.now + 60), "Seat")])
        for error, code, kind in [(rate, 429, "fair_use_limit"), (enforcement.SubscriptionAccessDenied(), 403, "subscription_access_denied")]:
            attempt = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await router.async_function_with_fallbacks(original_function=attempt, model="test", num_retries=4)
            attempt.assert_awaited_once()
            self.assertFalse(await record_failure(router, {}, error))
            with patch.object(router.cooldown_cache, "add_deployment_to_cooldown") as add:
                self.assertFalse(set_cooldown_deployments(router, error, code, deployment="test"))
                self.assertFalse(router.deployment_callback_on_failure({"exception": error}, None, None, None))
                add.assert_not_called()
            hooks = SimpleNamespace(post_call_failure_hook=AsyncMock(return_value=None),
                                    post_call_response_headers_hook=AsyncMock(return_value={}))
            with self.assertRaises(ProxyException) as mapped:
                await ProxyBaseLLMRequestProcessing({}).handle_llm_api_exception(error, UserAPIKeyAuth(), hooks)
            self.assertEqual(mapped.exception.to_dict()["type"], kind)
            self.assertEqual(mapped.exception.code, str(code))
            if code == 429:
                self.assertEqual(mapped.exception.headers["Retry-After"], rate.headers["Retry-After"])
                self.assertIn("5-hour", mapped.exception.message)
            else:
                self.assertEqual(mapped.exception.message, "No subscription is available to you for this model.")

    async def test_refresh_uses_live_codex_quota_independently_of_monitoring_cache(self):
        import httpx
        from litellm.proxy.extensions.chatgpt_subscriptions import tokens
        from litellm.proxy.extensions.chatgpt_subscriptions.service import UsageService

        account = self.accounts.get("a")
        tokens.write({"access_token": "synthetic", "account_id": "fixture-only", "expires_at": self.now + 8000}, account.path)
        def usage(used):
            window = {"used_percent": used, "limit_window_seconds": 18000, "reset_at": self.now + 900}
            return {"rate_limit": {"primary_window": window, "secondary_window": window}}

        payloads = [usage(10), {**usage(90), "additional_rate_limits": "ignored"}, usage(91)]
        account.transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payloads.pop(0)))
        monitoring = UsageService(account=account.name, path=account.path, transport=account.transport)
        watcher = fair_use.Quotas()
        self.assertEqual((await monitoring.get())["limits"][0]["primary"]["used_percent"], 10)
        for expected in (90, 91):
            await watcher.refresh(account)
            self.assertEqual(watcher.snapshots[self.ids["a"]]["primary"]["used_percent"], expected)
        self.assertEqual((await monitoring.get())["limits"][0]["primary"]["used_percent"], 10)
        self.assertEqual(payloads, [])

    def test_redis_stream_preserves_old_and_new_token_measurements(self):
        redis = Mock()
        pipe = Mock()
        redis.redis_client.pipeline.return_value = MagicMock()
        redis.redis_client.pipeline.return_value.__enter__.return_value = pipe
        self.enter(patch.object(self.store, "redis", return_value=redis))
        self.store.record("seat", ["new", self.now, "user", "t", "sol", [1, 2, 3], True])
        fields = pipe.xadd.call_args.args[1]
        self.assertEqual(json.loads(fields["event"]), ["new", self.now, "user", "t", 6, True])
        legacy = {b"event": json.dumps(["old", self.now, "user", "t", 4, True]).encode()}
        redis.redis_client.xrange.return_value = [(b"1-0", legacy), ("2-0", fields)]
        window = counters.Window(self.now - 60, self.now + 60)
        snapshot = self.store.snapshot("seat", window)
        self.assertEqual(snapshot["user"]["models"], {None: [4, 0, 0], "sol": [1, 2, 3]})

    async def test_history_rebuild_deduplicates_live_and_callback_snapshots(self):
        def row(identifier, started, count, ended=True, user="heavy", **fields):
            return {"user": user, "team_id": "t", "api_key": "a" * 64, "a": {
                "attempt_id": identifier, "started_at": utc(started), "ended_at": utc(self.now) if ended else None,
                "input_tokens": count, "output_tokens": None, **fields}}
        rows = [row("old", self.now - 5000, 500), row("one", self.now, 5, False, model="chatgpt/sol"),
                row("one", self.now, 12, model="openai/sol", cached_input_tokens=3, output_tokens=2),
                row("one", self.now, 12), row("two", self.now, None, user=None)]
        legacy = row("legacy", self.now, 5, user="early")
        legacy["a"]["started_at"] = None
        legacy["request_started"] = self.now
        rows.append(legacy)
        query = AsyncMock(return_value=rows)
        self.enter(patch.object(counters, "proxy_db", return_value=SimpleNamespace(db=SimpleNamespace(query_raw=query))))
        self.store.record("seat", ["one", self.now, "heavy", "t", "sol", [9, 3, 2], True])
        self.store.record("seat", ["three", self.now, "heavy", "t", "astra", [2, 6, 0], True])
        window = {"reset_at": self.now + 100, "window_seconds": 1000}
        for _ in range(2):
            counts = await self.store.counts("seat", "primary", window)
            self.assertEqual(counts["heavy"]["models"], {"sol": [9, 3, 2], "astra": [2, 6, 0]})
            self.assertEqual(counts["early"]["models"], {None: [5, 0, 0]})
            self.assertEqual(counts["key:" + "a"*64]["models"], {None: [0, 0, 0]})
        query.assert_awaited_once()
        await self.store.counts("seat", "primary", {**window, "reset_at": window["reset_at"] + 30})
        self.assertEqual(query.await_count, 2)
