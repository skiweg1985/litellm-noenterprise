"""Subscription attribution observed through real proxy requests and persisted log API."""

import time
import os
import uuid
import unittest
from datetime import datetime, timezone, timedelta
import test_subscription_models as model_tests


class SubscriptionLogTests(model_tests.SubscriptionModelTests):
    # Reuse setup helpers without inheriting the other acceptance scenarios.
    def logs(self, name, subscription_id=None, headers=None, **filters):
        now = datetime.now(timezone.utc)
        params = {
            "start_date": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": (now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "model_group": name,
            "page": 1,
            "page_size": 50,
        }
        if subscription_id:
            params["subscription_id"] = subscription_id
        params.update(filters)
        result = self.client.get("/spend/logs/ui", params=params, headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def wait_logs(self, name, count=1, subscription_id=None):
        for _ in range(80):
            result = self.logs(name, subscription_id)
            if len(result["data"]) >= count:
                return result
            time.sleep(0.5)
        self.fail("Expected persisted logs not visible")

    def setUp(self):
        super().setUp()
        self.control(statuses=[], stream_error=False, delay=0)

    def test_all_request_surfaces_have_one_attempt_and_unknown_filter_has_no_rows(self):
        import uuid

        account, _ = self.connect("Surfaces")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        for surface in ("chat/completions", "responses"):
            for stream in (False, True):
                body = {"model": name, "stream": stream}
                body.update(
                    {"input": "Synthetic " + str(stream)}
                    if surface == "responses"
                    else {
                        "messages": [
                            {"role": "user", "content": "Synthetic " + str(stream)}
                        ]
                    }
                )
                response = self.client.post("/v1/" + surface, json=body)
                self.assertEqual(response.status_code, 200, response.text)
        rows = self.wait_logs(name, 4)["data"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            attempts = row["metadata"].get("subscription_attempts", [])
            self.assertEqual(len(attempts), 1, row)
            self.assertEqual(attempts[0]["outcome"], "success")
            self.assertIsNotNone(attempts[0]["input_tokens"])
        self.assertEqual(
            len({r["metadata"]["subscription_logical_id"] for r in rows}), 4
        )
        self.assertEqual(self.logs(name, str(uuid.uuid4()))["total"], 0)
        self.assertEqual(self.logs(name, account["subscription_id"])["total"], 4)

    def test_failure_and_retry_attempts_are_preserved(self):
        account, _ = self.connect("Retries")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        self.control(statuses=[503, 200])
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "num_retries": 1,
                "messages": [{"role": "user", "content": "Retry synthetic"}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.wait_logs(name)["data"]
        attempts = rows[0]["metadata"].get("subscription_attempts", [])
        self.assertEqual([a["outcome"] for a in attempts], ["failure", "success"], rows)
        self.assertEqual([a["sequence"] for a in attempts], [1, 2])
        self.assertEqual([a["reason"] for a in attempts], ["initial", "retry"])
        self.assertIsNone(attempts[0]["input_tokens"])
        self.control(status=503, statuses=[])
        failed = self.client.post(
            "/v1/responses",
            json={"model": name, "num_retries": 0, "input": "Fail synthetic"},
        )
        self.assertGreaterEqual(failed.status_code, 400)
        rows = self.wait_logs(name, 2)["data"]
        failure = next(row for row in rows if row["status"] == "failure")
        self.assertEqual(
            [
                a["outcome"]
                for a in failure["metadata"].get("subscription_attempts", [])
            ],
            ["failure"],
            failure,
        )

    def test_forged_metadata_and_rename_do_not_change_historical_attribution(self):
        account, _ = self.connect("OriginalLabel")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        forged = {
            "subscription_ids": ["forged"],
            "subscription_attempts": [{"subscription_id": "forged"}],
            "subscription_logical_id": "forged",
        }
        for surface in ("chat/completions", "responses"):
            body = {"model": name, "metadata": forged, "litellm_metadata": forged}
            body.update(
                {"input": "Forged attribution"}
                if surface == "responses"
                else {"messages": [{"role": "user", "content": "Forged attribution"}]}
            )
            response = self.client.post(
                "/v1/" + surface,
                json=body,
                headers={"x-litellm-call-id": "same-client-id"},
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.client.patch(
            "/chatgpt/api/accounts/" + account["account"],
            json={"display_name": "Renamed"},
        ).raise_for_status()
        rows = self.wait_logs(name, 2)["data"]
        self.assertEqual(
            len({r["metadata"]["subscription_logical_id"] for r in rows}), 2
        )
        for row in rows:
            metadata = row["metadata"]
            self.assertNotEqual(metadata["subscription_logical_id"], "forged")
            self.assertEqual(metadata["subscription_ids"], [account["subscription_id"]])
            self.assertEqual(
                metadata["subscription_attempts"][0]["display_name"], "OriginalLabel"
            )
        self.assertEqual(self.logs(name, "forged")["total"], 0)

    def test_stream_failure_and_disconnect_keep_actual_attempt(self):
        account, _ = self.connect("Interrupted")
        for surface, mode in [
            ("responses", "failure"),
            ("responses", "cancelled"),
            ("chat/completions", "cancelled"),
        ]:
            name, created = self.create(account)
            self.assertEqual(created.status_code, 200, created.text)
            self.control(
                stream_error=mode == "failure",
                delay=0.1 if mode == "failure" else 0.5,
                clear=True,
            )
            body = {"model": name, "stream": True, "num_retries": 0}
            body.update(
                {"input": "Interrupted synthetic"}
                if surface == "responses"
                else {
                    "messages": [{"role": "user", "content": "Interrupted synthetic"}]
                }
            )
            with self.client.stream("POST", "/v1/" + surface, json=body) as response:
                self.assertEqual(response.status_code, 200)
                for line in response.iter_lines():
                    if mode == "cancelled" and line.startswith("data:"):
                        break
            row = self.wait_logs(name)["data"][0]
            attempts = row["metadata"].get("subscription_attempts", [])
            self.assertEqual(len(attempts), 1, row)
            self.assertEqual(attempts[0]["outcome"], mode, row)
            self.assertIsNone(attempts[0]["input_tokens"], row)
            self.assertEqual(len(self.control()["seen"]), 1)

    def test_filter_pagination_and_existing_user_scope(self):
        account, _ = self.connect("ScopedLogs")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        keys = []
        for _ in range(2):
            user = "log-user-" + uuid.uuid4().hex
            created_user = self.client.post(
                "/user/new", json={"user_id": user, "user_role": "internal_user"}
            )
            self.assertEqual(created_user.status_code, 200, created_user.text)
            team = self.client.post(
                "/team/new",
                json={
                    "team_alias": user,
                    "members_with_roles": [{"user_id": user, "role": "admin"}],
                },
            )
            self.assertEqual(team.status_code, 200, team.text)
            key = self.client.post(
                "/key/generate",
                json={
                    "user_id": user,
                    "team_id": team.json()["team_id"],
                    "models": [name],
                },
            ).json()["key"]
            keys.append({"Authorization": "Bearer " + key})
        for index, headers in enumerate([keys[0], keys[0], keys[1]]):
            response = self.client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": name,
                    "messages": [{"role": "user", "content": "Scoped " + str(index)}],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.wait_logs(name, 3)
        first = self.logs(name, account["subscription_id"], page_size=1)
        second = self.logs(name, account["subscription_id"], page_size=1, page=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual(len(first["data"]), 1)
        self.assertNotEqual(
            first["data"][0]["request_id"], second["data"][0]["request_id"]
        )
        own = self.logs(name, account["subscription_id"], headers=keys[0])
        self.assertEqual(own["total"], 2, own)
        other = self.logs(name, account["subscription_id"], headers=keys[1])
        self.assertEqual(other["total"], 1, other)
        self.assertTrue(
            set(r["request_id"] for r in own["data"]).isdisjoint(
                r["request_id"] for r in other["data"]
            )
        )

    def test_forgery_without_provider_dispatch_does_not_create_attribution(self):
        forged = {
            "subscription_ids": ["forged"],
            "subscription_attempts": [{"subscription_id": "forged"}],
            "subscription_logical_id": "forged",
        }
        name = "mock-log-" + uuid.uuid4().hex
        created = self.client.post(
            "/model/new",
            json={
                "model_name": name,
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": "fixture-unused",
                    "mock_response": "synthetic local reply",
                },
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.models.append(created.json()["model_id"])
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "messages": [{"role": "user", "content": "No subscription"}],
                "metadata": forged,
                "litellm_metadata": forged,
                "_subscription_attribution": forged,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        row = self.wait_logs(name)["data"][0]
        self.assertFalse(row["metadata"].get("subscription_attempts"), row)
        self.assertFalse(row["metadata"].get("subscription_ids"), row)
        self.assertEqual(self.logs(name, "forged")["total"], 0)
        account, _ = self.connect("RejectedLocally")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        self.client.post(
            "/chatgpt/api/accounts/" + account["account"] + "/logout"
        ).raise_for_status()
        self.control(clear=True)
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "num_retries": 0,
                "messages": [{"role": "user", "content": "No dispatch"}],
                "metadata": forged,
                "litellm_metadata": forged,
                "_subscription_attribution": forged,
            },
        )
        self.assertGreaterEqual(response.status_code, 400)
        row = self.wait_logs(name)["data"][0]
        self.assertFalse(row["metadata"].get("subscription_attempts"), row)
        self.assertEqual(self.control()["seen"], [])

    @unittest.skipUnless(
        os.getenv("RESPONSE_CACHE_ENABLED") == "true",
        "requires isolated response cache",
    )
    def test_cache_hit_does_not_claim_a_provider_attempt(self):
        account, _ = self.connect("CachedLogs")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        self.control(clear=True)
        body = {
            "model": name,
            "messages": [{"role": "user", "content": "Cache synthetic"}],
        }
        self.client.post("/v1/chat/completions", json=body).raise_for_status()
        self.wait_logs(name)
        forged = {
            "subscription_ids": ["forged"],
            "subscription_attempts": [{"subscription_id": "forged"}],
            "subscription_logical_id": "forged",
        }
        self.client.post(
            "/v1/chat/completions", json={**body, "metadata": forged}
        ).raise_for_status()
        rows = self.wait_logs(name, 2)["data"]
        self.assertEqual(len(self.control()["seen"]), 1)
        cached = [row for row in rows if row["cache_hit"] == "True"]
        self.assertEqual(len(cached), 1, rows)
        self.assertFalse(cached[0]["metadata"].get("subscription_attempts"), cached)
        self.assertFalse(cached[0]["metadata"].get("subscription_ids"), cached)
        self.assertEqual(self.logs(name, account["subscription_id"])["total"], 1)

    def test_success_has_dispatch_identity_and_preserves_model(self):
        account, _ = self.connect("LogPrimary")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "messages": [{"role": "user", "content": "Log synthetic hello"}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        row = self.wait_logs(name)["data"][0]
        attempts = row["metadata"].get("subscription_attempts", [])
        self.assertEqual(len(attempts), 1, row)
        attempt = attempts[0]
        self.assertEqual(attempt["subscription_id"], account["subscription_id"])
        self.assertEqual(attempt["display_name"], "LogPrimary")
        self.assertEqual(attempt["connection_revision"], account["connection_revision"])
        self.assertEqual(attempt["outcome"], "success")
        self.assertEqual(attempt["sequence"], 1)
        self.assertEqual(row["model_group"], name)
        self.assertNotIn(account["account"] + "/", row["model"])
        self.assertEqual(self.logs(name, account["subscription_id"])["total"], 1)


# Keep only the new log scenarios; inherited helpers remain available.
for _name in tuple(model_tests.SubscriptionModelTests.__dict__):
    if _name.startswith("test_"):
        setattr(SubscriptionLogTests, _name, None)
