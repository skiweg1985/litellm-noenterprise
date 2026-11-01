"""Preferred subscriptions and reserves through the installed proxy boundary."""

import os
import time
import uuid

import test_subscription_logs as log_tests
import test_subscription_models as model_tests


class SubscriptionReserveTests(log_tests.SubscriptionLogTests):
    def setUp(self):
        super().setUp()
        self.control(
            status_by_identity={}, retry_after_by_identity={}, stream_error_at=3
        )

    def create_reserve(self, accounts):
        name, created = self.create(accounts[0])
        self.assertEqual(created.status_code, 200, created.text)
        model_id = created.json()["model_id"]
        updated = self.client.patch(
            "/model/" + model_id + "/update",
            json={
                "model_info": {
                    "subscription_assignment": {
                        "mode": "priority",
                        "subscription_ids": [a["subscription_id"] for a in accounts],
                    }
                }
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        return name, model_id

    def test_paused_model_blocks_all_members_and_resumes_on_workers(self):
        from concurrent.futures import ThreadPoolExecutor

        first, first_identity = self.connect("PausedPrimary")
        second, _ = self.connect("PausedReserve")
        name, model_id = self.create_reserve([first, second])
        body = {"model": name, "input": "Pause synthetic"}
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.client.post("/model/block", json={"model_id": model_id}).raise_for_status()
        self.control(clear=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(
                pool.map(
                    lambda _: self.client.post("/v1/responses", json=body), range(16)
                )
            )
        self.assertTrue(
            all(r.status_code >= 400 for r in responses),
            [r.status_code for r in responses],
        )
        self.assertEqual(self.control()["seen"], [])
        self.client.post(
            "/model/unblock", json={"model_id": model_id}
        ).raise_for_status()
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(
                pool.map(
                    lambda _: self.client.post("/v1/responses", json=body), range(16)
                )
            )
        self.assertTrue(
            all(r.status_code == 200 for r in responses),
            [r.text for r in responses if r.status_code != 200],
        )
        self.assertEqual(self.control()["seen"], [first_identity] * 16)

    def test_outage_moves_to_reserve_and_never_escapes_membership(self):
        first, first_identity = self.connect("OutagePrimary")
        second, second_identity = self.connect("OutageReserve")
        name, model_id = self.create_reserve([first, second])
        self.control(status_by_identity={first_identity: 503}, clear=True)
        result = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "messages": [{"role": "user", "content": "Reserve synthetic"}],
                "num_retries": 9999,
                "fallbacks": [{name: ["fixture-model"]}],
                "disable_fallbacks": False,
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.control()["seen"], [first_identity, second_identity])
        row = self.wait_logs(name)["data"][0]
        attempts = row["metadata"]["subscription_attempts"]
        self.assertEqual(
            [a["subscription_id"] for a in attempts],
            [first["subscription_id"], second["subscription_id"]],
        )
        self.assertEqual([a["outcome"] for a in attempts], ["failure", "success"])
        self.assertEqual([a["reason"] for a in attempts], ["initial", "fallback"])
        self.assertEqual(row["model_id"], model_id)
        self.assertEqual(
            [a["routing_mode"] for a in attempts], ["priority", "priority"]
        )
        self.assertEqual([a["reserve_switch"] for a in attempts], [False, True])
        self.control(clear=True)
        again = self.client.post(
            "/v1/chat/completions",
            json={
                "model": name,
                "messages": [{"role": "user", "content": "Still cooling"}],
            },
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self.control()["seen"], [second_identity])

    def test_invalid_members_and_client_errors_do_not_spend_reserves(self):
        first, first_identity = self.connect("BadRequestPrimary")
        second, _ = self.connect("BadRequestReserve")
        name, model_id = self.create_reserve([first, second])
        for mode, ids in [
            ("priority", [first["subscription_id"]] * 2),
            ("fixed", [first["subscription_id"], second["subscription_id"]]),
            ("priority", [str(uuid.uuid4())]),
        ]:
            changed = self.client.patch(
                "/model/" + model_id + "/update",
                json={
                    "model_info": {
                        "subscription_assignment": {
                            "mode": mode,
                            "subscription_ids": ids,
                        }
                    }
                },
            )
            self.assertEqual(changed.status_code, 400, changed.text)
        self.control(status_by_identity={first_identity: 400}, clear=True)
        response = self.client.post(
            "/v1/responses",
            json={"model": name, "input": "Invalid synthetic", "num_retries": 999},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.control()["seen"], [first_identity])
        self.control(status_by_identity={}, clear=True)
        for account in (first, second):
            self.client.post(
                "/chatgpt/api/accounts/" + account["account"] + "/logout"
            ).raise_for_status()
        response = self.client.post(
            "/v1/responses", json={"model": name, "input": "Unavailable synthetic"}
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.control()["seen"], [])
        self.assertIn("Reconnect", response.text)

    def test_rate_limit_retry_after_and_preferred_recovery(self):
        first, first_identity = self.connect("LimitedPrimary")
        second, second_identity = self.connect("LimitedReserve")
        name, model_id = self.create_reserve([first, second])
        self.client.patch(
            "/model/" + model_id + "/update", json={"model_info": {"cooldown_time": 1}}
        ).raise_for_status()
        self.control(
            status_by_identity={first_identity: 429},
            retry_after_by_identity={first_identity: 3},
            clear=True,
        )
        body = {"model": name, "input": "Rate limit synthetic"}
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.assertEqual(self.control()["seen"], [first_identity, second_identity])
        self.control(status_by_identity={}, clear=True)
        time.sleep(1.2)
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.assertEqual(self.control()["seen"], [second_identity])
        time.sleep(2.1)
        self.control(clear=True)
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.assertEqual(self.control()["seen"], [first_identity])

    def test_rejected_credentials_require_recovery_before_reuse(self):
        first, first_identity = self.connect("RejectedPrimary")
        second, second_identity = self.connect("RejectedReserve")
        name, _ = self.create_reserve([first, second])
        self.control(status_by_identity={first_identity: 401}, clear=True)
        body = {"model": name, "input": "Credential synthetic"}
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.assertEqual(self.control()["seen"], [first_identity, second_identity])
        state = next(
            a
            for a in self.client.get("/chatgpt/api/accounts").json()["accounts"]
            if a["subscription_id"] == first["subscription_id"]
        )
        self.assertEqual(state["state"], "credential_rejected")
        self.control(status_by_identity={}, clear=True)
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.assertEqual(self.control()["seen"], [second_identity])
        self.control(identity=first_identity)
        self.client.post(
            "/chatgpt/api/accounts/" + first["account"] + "/login"
        ).raise_for_status()
        for _ in range(100):
            state = next(
                a
                for a in self.client.get("/chatgpt/api/accounts").json()["accounts"]
                if a["subscription_id"] == first["subscription_id"]
            )
            if state["token_valid"]:
                break
            time.sleep(0.1)
        self.assertTrue(state["token_valid"])
        self.control(clear=True)
        self.client.post("/v1/responses", json=body).raise_for_status()
        self.assertEqual(self.control()["seen"], [first_identity])

    def test_stream_lifecycle_content_and_cancellation_never_switch_accounts(self):
        first, first_identity = self.connect("StreamingPrimary")
        second, _ = self.connect("StreamingReserve")
        for surface in ("responses", "chat/completions"):
            for boundary in ("lifecycle", "content", "cancel"):
                name, _ = self.create_reserve([first, second])
                self.control(
                    stream_error=boundary != "cancel",
                    stream_error_at=3 if boundary == "lifecycle" else 6,
                    delay=0.1 if boundary != "cancel" else 0.3,
                    clear=True,
                )
                body = {"model": name, "stream": True}
                body.update(
                    {"input": "Stream boundary synthetic"}
                    if surface == "responses"
                    else {
                        "messages": [
                            {"role": "user", "content": "Stream boundary synthetic"}
                        ]
                    }
                )
                with self.client.stream(
                    "POST", "/v1/" + surface, json=body
                ) as response:
                    self.assertIn(
                        response.status_code,
                        (200, 500)
                        if boundary == "lifecycle" and surface == "chat/completions"
                        else (200,),
                    )
                    delivered = []
                    for line in response.iter_lines():
                        delivered.append(line)
                        if boundary == "cancel" and line.startswith("data:"):
                            break
                if boundary == "content":
                    self.assertIn("ok", "\n".join(delivered))
                row = self.wait_logs(name)["data"][0]
                attempts = row["metadata"]["subscription_attempts"]
                self.assertEqual(len(attempts), 1, row)
                self.assertEqual(
                    attempts[0]["outcome"],
                    "cancelled" if boundary == "cancel" else "failure",
                    row,
                )
                self.assertEqual(self.control()["seen"], [first_identity])
                self.assertFalse(attempts[0]["reserve_switch"])
                self.control(stream_error=False, delay=0)

    def test_caller_timeout_does_not_cool_down_or_consume_reserves(self):
        first, first_identity = self.connect("TimeoutPrimary")
        second, _ = self.connect("TimeoutReserve")
        name, _ = self.create_reserve([first, second])
        self.control(delay=0.3, clear=True)
        response = self.client.post(
            "/v1/responses",
            json={"model": name, "input": "Caller timeout synthetic", "timeout": 0.05},
        )
        self.assertIn(response.status_code, (408, 504), response.text)
        self.assertEqual(self.control()["seen"], [first_identity])
        self.control(delay=0, clear=True)
        self.client.post(
            "/v1/responses",
            json={"model": name, "input": "Recovered timeout synthetic"},
        ).raise_for_status()
        self.assertEqual(self.control()["seen"], [first_identity])

    def test_reserve_assignment_survives_restart(self):
        if os.getenv("SUBSCRIPTION_RESTART_PHASE") != "read":
            first, _ = self.connect("ReservePersistencePrimary")
            second, _ = self.connect("ReservePersistenceBackup")
            name, model_id = self.create_reserve([first, second])
            self.client.patch(
                "/model/" + model_id + "/update",
                json={"model_name": "reserve-restart-" + first["subscription_id"]},
            ).raise_for_status()
            self.models.remove(model_id)
            return
        accounts = self.client.get("/chatgpt/api/accounts").json()["accounts"]
        first = next(
            a for a in accounts if a["display_name"] == "ReservePersistencePrimary"
        )
        second = next(
            a for a in accounts if a["display_name"] == "ReservePersistenceBackup"
        )
        name = "reserve-restart-" + first["subscription_id"]
        self.control(status_by_identity={first["account_id"]: 503}, clear=True)
        self.client.post(
            "/v1/responses", json={"model": name, "input": "Restored reserves"}
        ).raise_for_status()
        self.assertEqual(
            self.control()["seen"], [first["account_id"], second["account_id"]]
        )
        model = next(
            m
            for m in self.client.get("/v1/model/info").json()["data"]
            if m["model_name"] == name
        )
        self.assertEqual(
            model["model_info"]["subscription_assignment"]["subscription_ids"],
            [first["subscription_id"], second["subscription_id"]],
        )
        self.models.append(model["model_info"]["id"])

    def test_preferred_subscription_is_used_without_changing_another_model(self):
        first, first_identity = self.connect("Preferred")
        second, second_identity = self.connect("Reserve")
        name, model_id = self.create_reserve([first, second])
        unrelated, created = self.create(second)
        self.assertEqual(created.status_code, 200, created.text)
        for model in (name, unrelated, name):
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "user", "content": "Ordered synthetic " + model}
                    ],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.control()["seen"], [first_identity, second_identity, first_identity]
        )
        rows = self.wait_logs(name, 2)["data"]
        self.assertTrue(
            all(
                row["metadata"]["subscription_ids"] == [first["subscription_id"]]
                for row in rows
            )
        )
        self.assertTrue(all(row["model_id"] == model_id for row in rows))


for _parent in (log_tests.SubscriptionLogTests, model_tests.SubscriptionModelTests):
    for _name in _parent.__dict__:
        if _name.startswith("test_"):
            setattr(SubscriptionReserveTests, _name, None)

del _parent
