import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from litellm.proxy.extensions.chatgpt_subscriptions.prtg import LOOKUP, duration, render
from litellm.proxy.extensions.chatgpt_subscriptions.routes import register
from litellm.proxy.extensions.chatgpt_subscriptions.service import (
    ModelsService,
    MonitoringError,
    UsageService,
    normalize,
    read_credentials,
)



def payload():
    window = {
        "used_percent": 23,
        "limit_window_seconds": 18000,
        "reset_at": int(time.time()) + 1000,
    }
    return {
        "rate_limit": {
            "allowed": True,
            "primary_window": window,
            "secondary_window": {**window, "limit_window_seconds": 604800},
        },
        "additional_rate_limits": [
            {
                "metered_feature": "extra",
                "rate_limit": {"allowed": False, "primary_window": window},
            }
        ],
    }


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.json"
        self.path.write_text(
            json.dumps(
                {
                    "access_token": "secret-access-marker",
                    "account_id": "test-account",
                    "expires_at": time.time() + 3600,
                    "refresh_token": "secret-refresh-marker",
                }
            )
        )
        self.before = self.path.read_bytes()
        self.stat = self.path.stat().st_mtime_ns
        env = patch.dict(
            os.environ,
            {
                "CHATGPT_TOKEN_DIR": self.tmp.name,
                "CHATGPT_AUTH_FILE": "auth.json",
                "CHATGPT_MONITORING_API_KEY": "monitor-secret",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def tearDown(self):
        # Individual malformed-file tests restore their fixture before completion.
        self.assertEqual(self.before, self.path.read_bytes())
        self.assertEqual(self.stat, self.path.stat().st_mtime_ns)

    def service(self, status=200, data=None):
        def handle(req):
            self.calls.append(req)
            self.assertEqual(str(req.url), "https://chatgpt.com/backend-api/wham/usage")
            self.assertEqual(req.method, "GET")
            self.assertEqual(
                req.headers["Authorization"], "Bearer secret-access-marker"
            )
            self.assertEqual(req.headers["ChatGPT-Account-Id"], "test-account")
            return httpx.Response(status, json=data if data is not None else payload())

        return UsageService(httpx.MockTransport(handle))

    def client(self, service):
        app = FastAPI()

        @app.get("/health/liveliness")
        def health():
            return {"ok": True}

        @app.post("/responses")
        def model(authorization: str = Header(default="")):
            if authorization != "Bearer model-secret":
                raise HTTPException(401)
            return {"ok": True}

        register(app, services={"default": service})
        return TestClient(app)

    def test_auth_routes_and_channels(self):
        client = self.client(self.service())
        path = "/chatgpt/accounts/default/usage"
        self.assertEqual(client.get(path).status_code, 401)
        self.assertEqual(
            client.get(path, headers={"X-Monitoring-Key": "wrong"}).status_code, 401
        )
        self.assertFalse(self.calls)
        headers = {"X-Monitoring-Key": "monitor-secret"}
        result = client.get(path + "/prtg", headers=headers)
        self.assertEqual(result.status_code, 200)
        channels = result.json()["prtg"]["result"]
        self.assertEqual(len(channels), 6)
        self.assertEqual(channels[0]["valuelookup"], LOOKUP)
        self.assertEqual(channels[1]["value"], 77)
        self.assertEqual(channels[2]["unit"], "TimeSeconds")
        self.assertEqual(client.get("/health/liveliness").status_code, 200)
        self.assertEqual(client.post("/responses", headers=headers).status_code, 401)
        self.assertEqual(
            client.post(
                "/responses", headers={"Authorization": "Bearer model-secret"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.get(path, headers=headers).headers["cache-control"], "no-store"
        )
        self.assertEqual(len(self.calls), 1)
        with patch.dict(os.environ, {"CHATGPT_MONITORING_API_KEY": ""}):
            self.assertEqual(client.get(path, headers=headers).status_code, 503)

    def test_error_contract_no_secrets(self):
        for status in [401, 429, 500, 302]:
            client = self.client(
                self.service(status, {"secret": "secret-access-marker"})
            )
            headers = {"X-Monitoring-Key": "monitor-secret"}
            response = client.get(
                "/chatgpt/accounts/default/usage/prtg", headers=headers
            )
            self.assertEqual(response.json()["prtg"]["error"], 1)
            self.assertNotIn("secret-access-marker", response.text)
            self.assertEqual(
                client.get(
                    "/chatgpt/accounts/default/usage", headers=headers
                ).status_code,
                503,
            )

    def test_read_errors_do_not_write(self):
        for data in [
            {},
            [],
            {"access_token": "x", "expires_at": 1},
            {"access_token": "x", "expires_at": True},
        ]:
            with (
                patch("pathlib.Path.read_text", return_value=json.dumps(data)),
                self.assertRaises(MonitoringError),
            ):
                read_credentials()
        with patch("pathlib.Path.read_text", side_effect=OSError("secret-marker")):
            with self.assertRaises(MonitoringError) as caught:
                read_credentials()
            self.assertNotIn("secret-marker", str(caught.exception))

    def test_groups_and_stable_channels(self):
        snapshot = normalize(payload())
        out = render(snapshot, "extra", "primary")["prtg"]["result"]
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0]["value"], 0)
        for group, windows in [
            ("missing", "primary"),
            ("extra", "primary,secondary"),
            ("codex", "bad"),
        ]:
            with self.assertRaises(MonitoringError):
                render(snapshot, group, windows)
        before = [
            x["channel"]
            for x in render(snapshot, "codex", "primary,secondary")["prtg"]["result"]
        ]
        snapshot["limits"][0]["primary"]["window_seconds"] = 1234
        after = [
            x["channel"]
            for x in render(snapshot, "codex", "primary,secondary")["prtg"]["result"]
        ]
        self.assertEqual(before, after)
        self.assertEqual(
            before,
            [
                "Allowed",
                "Primary Available",
                "Primary Reset",
                "Secondary Available",
                "Secondary Reset",
                "Data Age",
            ],
        )
        self.assertIsNone(normalize({})["limits"][0]["primary"])
        for value in [True, float("nan"), "23"]:
            bad = payload()
            bad["rate_limit"]["primary_window"]["used_percent"] = value
            with self.assertRaises(MonitoringError):
                normalize(bad)
        missing = payload()
        missing["rate_limit"]["primary_window"].pop("used_percent")
        missing = normalize(missing)
        self.assertIsNone(missing["limits"][0]["primary"]["remaining_percent"])
        with self.assertRaises(MonitoringError):
            render(missing, "codex", "primary")
        for used, expected in [(105, 0), (-10, 100)]:
            data = payload()
            data["rate_limit"]["primary_window"]["used_percent"] = used
            self.assertEqual(
                normalize(data)["limits"][0]["primary"]["remaining_percent"], expected
            )

    def test_sensor_text(self):
        data = payload()
        summer = datetime(2026, 9, 25, 23, 20, 15, tzinfo=timezone.utc).timestamp()
        winter = datetime(2026, 12, 27, 14, 4, 22, tzinfo=timezone.utc).timestamp()
        data["rate_limit"]["primary_window"]["reset_at"] = summer
        data["rate_limit"]["secondary_window"]["used_percent"] = 18.5
        data["rate_limit"]["secondary_window"]["reset_at"] = winter
        snapshot = normalize(data)
        self.assertEqual(
            render(snapshot, "codex", "primary,secondary")["prtg"]["text"],
            "Primary 5h: 77% left, resets Sep 25 23:20 UTC"
            " | Secondary 7d: 81.5% left, resets Dec 27 14:04 UTC",
        )
        self.assertEqual(
            render(snapshot, "codex", "secondary")["prtg"]["text"],
            "Secondary 7d: 81.5% left, resets Dec 27 14:04 UTC",
        )
        for seconds, expected in [(18000, "5h"), (604800, "7d"), (900, "15min")]:
            self.assertEqual(duration(seconds), expected)
        self.assertEqual(duration(1234), "1234s")
        self.assertEqual(duration(5400.0), "90min")

    def test_concurrent_cache_and_expiry(self):
        async def check():
            service = self.service()
            await asyncio.gather(*(service.get() for _ in range(10)))
            self.assertEqual(len(self.calls), 1)
            service.until = 0
            await service.get()
            self.assertEqual(len(self.calls), 2)
            service.until = 0

            async def fail():
                raise MonitoringError("Unavailable.")

            service.fetch = fail
            for _ in range(2):
                with self.assertRaises(MonitoringError):
                    await service.get()
            self.assertIsNone(service.cached)

        asyncio.run(check())

    def test_transport_timeout_is_redacted(self):
        def fail(request):
            raise httpx.ReadTimeout("secret-access-marker")

        async def check():
            with self.assertRaises(MonitoringError) as caught:
                await UsageService(httpx.MockTransport(fail)).get()
            self.assertNotIn("secret-access-marker", str(caught.exception))

        asyncio.run(check())

    def test_expired_token_never_calls_openai(self):
        service = self.service()

        async def check():
            with (
                patch(
                    "pathlib.Path.read_text",
                    return_value=json.dumps(
                        {
                            "access_token": "secret-access-marker",
                            "expires_at": 1,
                            "refresh_token": "secret-refresh-marker",
                            "account_id": "test-account",
                        }
                    ),
                ),
                self.assertRaises(MonitoringError),
            ):
                await service.get()
            self.assertEqual(self.calls, [])

        asyncio.run(check())

    def test_invalid_json_never_leaks_body(self):
        service = UsageService(
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text="secret-refresh-marker not json"
                )
            )
        )

        async def check():
            with self.assertRaises(MonitoringError) as caught:
                await service.get()
            self.assertNotIn("secret-refresh-marker", str(caught.exception))

        asyncio.run(check())

    def test_collision(self):
        app = FastAPI()
        register(app)
        with self.assertRaises(RuntimeError):
            register(app)


    def models_service(self, status=200, data=None):
        def handle(req):
            self.calls.append(req)
            self.assertEqual(
                str(req.url), "https://chatgpt.com/backend-api/codex/models?client_version=0.159.2"
            )
            self.assertEqual(req.headers["Authorization"], "Bearer secret-access-marker")
            self.assertEqual(req.headers["ChatGPT-Account-ID"], "test-account")
            self.assertEqual(req.headers["originator"], "codex_cli_rs")
            self.assertEqual(req.headers["version"], "0.159.2")
            return httpx.Response(status, json=data if data is not None else models_payload())

        return ModelsService(httpx.MockTransport(handle))

    def test_models_in_picker_order_without_cli_instructions(self):
        async def check():
            result = await self.models_service().get()
            self.assertEqual(
                [(m["slug"], m["name"], m["visible"], m["priority"]) for m in result["models"]],
                [("gpt-6.1-sol", "GPT-6.1-Sol", True, 0), ("gpt-6-luna", "GPT-6-Luna", True, 4),
                 ("gpt-reserve", "GPT-Reserve", False, 4)],
            )
            self.assertEqual(result["models"][0]["reasoning"], ["low", "medium", "ultra"])
            self.assertEqual(result["models"][0]["context_window"], 272000)
            self.assertNotIn("base_instructions", json.dumps(result))

        asyncio.run(check())

    def test_models_are_cached_for_five_minutes(self):
        service = self.models_service()

        async def check():
            await service.get()
            await service.get()
            self.assertEqual(len(self.calls), 1)
            self.assertGreater(service.until - time.monotonic(), 290)

        asyncio.run(check())

    def test_models_errors_are_fixed_messages(self):
        async def check():
            with self.assertRaises(MonitoringError) as caught:
                await self.models_service(status=401).get()
            self.assertIn("Renew it through LiteLLM", str(caught.exception))
            for bad in ({"models": "secret-refresh-marker"}, {"models": [{"slug": 5}]}):
                with self.assertRaises(MonitoringError) as caught:
                    await self.models_service(data=bad).get()
                self.assertNotIn("secret-refresh-marker", str(caught.exception))

        asyncio.run(check())


def models_payload():
    entry = {"visibility": "list", "context_window": 272000, "base_instructions": "x" * 1000,
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "ultra"}]}
    return {"models": [
        {**entry, "slug": "gpt-reserve", "display_name": "GPT-Reserve", "priority": 4, "visibility": "hide"},
        {**entry, "slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "priority": 4},
        {**entry, "slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "priority": 0},
    ]}



if __name__ == "__main__":
    unittest.main()
