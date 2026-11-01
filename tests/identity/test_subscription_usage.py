"""Observed subscription usage through the real proxy and retained log store."""

import asyncio
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import test_subscription_logs as log_tests


class SubscriptionUsageTests(log_tests.SubscriptionLogTests):
    def usage(self, subscription_id=None, start=None, end=None, headers=None):
        now = datetime.now(timezone.utc)
        params = {
            "start_date": (start or now - timedelta(hours=1)).isoformat(),
            "end_date": (end or now + timedelta(minutes=1)).isoformat(),
        }
        if subscription_id:
            params["subscription_id"] = subscription_id
        return self.client.get(
            "/chatgpt/api/observed-usage", params=params, headers=headers
        )

    def test_success_is_counted_from_retained_attempts_and_links_match_logs(self):
        account, _ = self.connect("ObservedOriginal")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        response = self.client.post(
            "/v1/responses", json={"model": name, "input": "Observed synthetic request"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.wait_logs(name)
        self.client.patch(
            "/chatgpt/api/accounts/" + account["account"],
            json={"display_name": "Observed renamed"},
        ).raise_for_status()
        response = self.usage(account["subscription_id"])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(len(result["subscriptions"]), 1)
        row = result["subscriptions"][0]
        self.assertEqual(row["display_name"], "Observed renamed")
        self.assertEqual(row["historical_names"], ["ObservedOriginal"])
        self.assertEqual(
            (
                row["logical_requests"],
                row["provider_attempts"],
                row["failed_attempts"],
                row["reserve_switches"],
            ),
            (1, 1, 0, 0),
        )
        self.assertEqual(
            (row["input_tokens"], row["output_tokens"], row["unknown_input_attempts"]),
            (2, 2, 0),
        )
        self.assertEqual(
            row["revisions"][0]["connection_revision"], account["connection_revision"]
        )
        bounds = {
            key: result[key].replace("T", " ").removesuffix("Z")
            for key in ("start_date", "end_date")
        }
        self.assertEqual(
            self.logs(name, account["subscription_id"], **bounds)["total"],
            row["logical_requests"],
        )

    def seed_logs(self, rows):
        """Arrange historical persisted callbacks; assertions use only the public API."""

        async def insert():
            from prisma import Json, Prisma

            db = Prisma()
            await db.connect()
            try:
                for row in rows:
                    await db.litellm_spendlogs.create(
                        data={**row, "metadata": Json(row.get("metadata", {}))}
                    )
            finally:
                await db.disconnect()

        asyncio.run(insert())

    def test_duplicate_snapshots_unknown_usage_and_retained_legacy_are_explicit(self):
        first, _ = self.connect("HistoricalPrimary")
        second, _ = self.connect("HistoricalReserve")
        self.client.post(
            "/chatgpt/api/accounts/" + first["account"] + "/retire"
        ).raise_for_status()
        start = datetime(2040, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        logical = str(uuid.uuid4())

        def attempt(account, **fields):
            return {
                "attempt_id": str(uuid.uuid4()),
                "subscription_id": account["subscription_id"],
                "display_name": account["display_name"],
                "connection_revision": account["connection_revision"],
                "outcome": "success",
                "input_tokens": 4,
                "output_tokens": 6,
                "reserve_switch": False,
                "ended_at": "2040-01-01T00:00:10Z",
                **fields,
            }

        failed = attempt(
            first, outcome="failure", input_tokens=None, output_tokens=None
        )
        reserve = attempt(second, reserve_switch=True)
        unknown = attempt(
            second,
            connection_revision="replacement-revision",
            input_tokens=None,
            output_tokens=None,
        )

        def row(attempts, **fields):
            return {
                "request_id": str(uuid.uuid4()),
                "call_type": "completion",
                "custom_llm_provider": "chatgpt",
                "startTime": start,
                "endTime": start + timedelta(seconds=1),
                "metadata": {
                    "subscription_logical_id": logical,
                    "subscription_ids": list({a["subscription_id"] for a in attempts}),
                    "subscription_attempts": attempts,
                },
                **fields,
            }

        self.seed_logs(
            [
                row(
                    [
                        failed,
                        {
                            **reserve,
                            "outcome": "incomplete",
                            "ended_at": None,
                            "input_tokens": None,
                            "output_tokens": None,
                        },
                    ]
                ),
                row([failed, reserve]),
                row([failed, reserve]),
                row(
                    [unknown],
                    metadata={
                        "subscription_logical_id": str(uuid.uuid4()),
                        "subscription_ids": [second["subscription_id"]],
                        "subscription_attempts": [unknown],
                    },
                ),
                row([reserve], cache_hit="true"),
                row(
                    [], model="chatgpt/model__" + second["subscription_id"], metadata={}
                ),
                row([], custom_llm_provider="", metadata={}),
                row([], custom_llm_provider="openai", metadata={}),
                row([attempt(second)], startTime=end + timedelta(seconds=1)),
            ]
        )
        response = self.usage(start=start, end=end)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        own = {r["subscription_id"]: r for r in result["subscriptions"]}
        primary, used = own[first["subscription_id"]], own[second["subscription_id"]]
        self.assertTrue(primary["retired"])
        self.assertEqual(
            (
                primary["logical_requests"],
                primary["provider_attempts"],
                primary["failed_attempts"],
                primary["input_tokens"],
            ),
            (1, 1, 1, None),
        )
        self.assertEqual(
            (
                used["logical_requests"],
                used["provider_attempts"],
                used["reserve_switches"],
            ),
            (2, 2, 1),
        )
        self.assertEqual(
            (
                used["input_tokens"],
                used["output_tokens"],
                used["unknown_input_attempts"],
                used["unknown_output_attempts"],
            ),
            (4, 6, 1, 1),
        )
        self.assertEqual(
            {r["connection_revision"] for r in used["revisions"]},
            {second["connection_revision"], "replacement-revision"},
        )
        self.assertEqual(result["coverage"]["unattributed_log_records"], 1)
        self.assertEqual(result["coverage"]["cache_hit_log_records"], 1)
        self.assertEqual(result["coverage"]["unknown_provider_log_records"], 1)
        self.assertFalse(result["coverage"]["history_complete"])
        self.assertIsNotNone(result["coverage"]["earliest_retained_log_at"])
        empty = self.usage(
            second["subscription_id"],
            start=end + timedelta(days=1),
            end=end + timedelta(days=2),
        ).json()["subscriptions"][0]
        self.assertEqual(
            (
                empty["logical_requests"],
                empty["provider_attempts"],
                empty["input_tokens"],
            ),
            (0, 0, None),
        )
        self.assertNotIn("account_id", response.text)
        self.assertNotIn("access_token", response.text)

    def test_only_native_or_browser_admins_can_observe_usage(self):
        key = self.client.post("/key/generate", json={}).json()["key"]
        denied = self.usage(headers={"Authorization": "Bearer " + key})
        self.assertIn(denied.status_code, (401, 403), denied.text)
        token = httpx.post(
            "http://issuer:8080/control/access-token",
            json={"oid": "usage-provider-admin", "roles": ["access", "admin"]},
            trust_env=False,
        ).json()["access_token"]
        denied = self.usage(headers={"Authorization": "Bearer " + token})
        self.assertEqual(denied.status_code, 403, denied.text)
        denied = self.client.get(
            "/chatgpt/api/observed-usage",
            params={
                "start_date": "2040-01-01T00:00:00",
                "end_date": "2040-01-02T00:00:00",
            },
        )
        self.assertEqual(denied.status_code, 400, denied.text)

    def test_replacing_connection_keeps_separate_revision_observations(self):
        account, identity = self.connect("UsageReplacement")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        body = {"model": name, "input": "Before and after replacement"}
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.wait_logs(name)
        self.control(identity="replacement-" + uuid.uuid4().hex)
        base = "/chatgpt/api/accounts/" + account["account"]
        self.client.post(base + "/login").raise_for_status()
        for _ in range(100):
            current = next(
                row
                for row in self.client.get("/chatgpt/api/accounts").json()["accounts"]
                if row["subscription_id"] == account["subscription_id"]
            )
            if current.get("pending_account"):
                break
            time.sleep(0.1)
        else:
            self.fail("Replacement did not await confirmation")
        self.client.post(base + "/pending/confirm").raise_for_status()
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.wait_logs(name, 2)
        result = self.usage(account["subscription_id"])
        self.assertEqual(result.status_code, 200, result.text)
        row = result.json()["subscriptions"][0]
        self.assertEqual(
            (
                row["logical_requests"],
                row["provider_attempts"],
                row["input_tokens"],
                row["output_tokens"],
            ),
            (2, 2, 4, 4),
        )
        self.assertEqual(
            len({revision["connection_revision"] for revision in row["revisions"]}), 2
        )
        self.assertNotIn(identity, result.text)

    def test_actual_reserve_request_counts_each_used_subscription_once(self):
        first, first_identity = self.connect("UsagePreferred")
        second, _ = self.connect("UsageReserve")
        name, created = self.create(first)
        self.assertEqual(created.status_code, 200, created.text)
        self.client.patch(
            "/model/" + created.json()["model_id"] + "/update",
            json={
                "model_info": {
                    "subscription_assignment": {
                        "mode": "priority",
                        "subscription_ids": [
                            first["subscription_id"],
                            second["subscription_id"],
                        ],
                    }
                }
            },
        ).raise_for_status()
        self.control(status_by_identity={first_identity: 503})
        try:
            response = self.client.post(
                "/v1/responses",
                json={"model": name, "input": "Observed reserve request"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.wait_logs(name)
            primary = self.usage(first["subscription_id"]).json()["subscriptions"][0]
            reserve = self.usage(second["subscription_id"]).json()["subscriptions"][0]
            self.assertEqual(
                (
                    primary["logical_requests"],
                    primary["provider_attempts"],
                    primary["failed_attempts"],
                    primary["input_tokens"],
                ),
                (1, 1, 1, None),
            )
            self.assertEqual(
                (
                    reserve["logical_requests"],
                    reserve["provider_attempts"],
                    reserve["reserve_switches"],
                    reserve["input_tokens"],
                ),
                (1, 1, 1, 2),
            )
        finally:
            self.control(status_by_identity={})


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in SubscriptionUsageTests.__dict__:
        if name.startswith("test_"):
            suite.addTest(SubscriptionUsageTests(name))
    return suite
