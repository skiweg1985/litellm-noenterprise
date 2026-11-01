"""Synthetic, offline tests of the opt-in OpenRouter classifier."""
import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from litellm.router_strategy.complexity_router import openrouter_jev as jev


class OpenRouterJevTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.reply = httpx.Response(200, json={"answers": {"tier": {"choice": "COMPLEX", "confidence": 0.8}}})
        self.client = httpx.AsyncClient(base_url=jev.OPENROUTER_API_BASE, transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(self.client.aclose)
        self.plugin = jev.JevTierClassifier()
        client_patch = patch.object(self.plugin, "_client", return_value=self.client)
        client_patch.start(); self.addCleanup(client_patch.stop)
        credential_patch = patch.object(jev.CredentialAccessor, "get_credential_values", return_value={"api_key": "synthetic-db-key"})
        self.credential = credential_patch.start(); self.addCleanup(credential_patch.stop)
        env = patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-env-key"})
        env.start(); self.addCleanup(env.stop)

    def handle(self, request):
        self.calls.append(request)
        return self.reply

    async def classify(self, *messages):
        return await self.plugin.classify(SimpleNamespace(structured_messages=list(messages)))

    async def test_scoped_user_context_and_stored_credential(self):
        result = await self.classify(
            {"role": "system", "content": "not sent"},
            {"role": "user", "content": "oldest"},
            {"role": "user", "content": "previous one"},
            {"role": "assistant", "content": "not sent"},
            {"role": "user", "content": "previous two"},
            {"role": "user", "content": [{"type": "text", "text": "current"}, {"type": "image_url", "image_url": "not sent"}]},
        )
        self.assertEqual(result, "COMPLEX")
        request = self.calls[0]
        self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(request.headers["authorization"], "Bearer synthetic-db-key")
        self.assertEqual(json.loads(request.content)["state"], {"current_request": "current", "previous_requests": ["previous one", "previous two"]})

    async def test_rotation_and_environment_fallback(self):
        self.credential.return_value = {"api_key": "rotated-synthetic"}
        await self.classify({"role": "user", "content": "test"})
        self.credential.return_value = {}
        await self.classify({"role": "user", "content": "test"})
        self.assertEqual([request.headers["authorization"] for request in self.calls], ["Bearer rotated-synthetic", "Bearer synthetic-env-key"])

    async def test_declines_without_credentials_or_text(self):
        self.credential.return_value = {}
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(await self.classify({"role": "user", "content": "test"}))
        self.assertIsNone(await self.classify({"role": "assistant", "content": "not sent"}))
        self.assertEqual(self.calls, [])

    async def test_threshold_and_unknown_tier_decline(self):
        with patch.object(jev, "MIN_CONFIDENCE", 0.9):
            self.assertIsNone(await self.classify({"role": "user", "content": "test"}))
        self.reply = httpx.Response(200, json={"answers": {"tier": {"choice": "UNKNOWN"}}})
        self.assertIsNone(await self.classify({"role": "user", "content": "test"}))

    async def test_request_text_is_bounded(self):
        await self.classify({"role": "user", "content": "x" * 5000})
        self.assertEqual(len(json.loads(self.calls[0].content)["state"]["current_request"]), 4000)

    async def test_plugin_is_deepcopyable_after_client_creation(self):
        plugin = jev.JevTierClassifier()
        client = plugin._client()
        self.addAsyncCleanup(client.aclose)
        self.addCleanup(jev._CLIENTS.clear)
        self.assertIsInstance(copy.deepcopy(plugin), jev.JevTierClassifier)

    async def test_http_errors_propagate_for_router_fallback(self):
        self.reply = httpx.Response(429, json={"error": "synthetic limit"})
        with self.assertRaises(httpx.HTTPStatusError):
            await self.classify({"role": "user", "content": "test"})


if __name__ == "__main__":
    unittest.main()
