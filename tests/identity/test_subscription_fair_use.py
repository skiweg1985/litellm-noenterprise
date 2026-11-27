"""Team grants and measured fair use through the live proxy and ChatGPT fixture."""
import time
import uuid

import test_subscription_models as models
import test_subscription_distribution as distribution


class SubscriptionFairUseTests(models.SubscriptionModelTests):
    create_distribution = distribution.SubscriptionDistributionTests.create_distribution

    def test_routing_changes_only_for_the_caller_over_share(self):
        suffix = uuid.uuid4().hex[:6]
        first, seat1 = self.connect("FairUseFirst" + suffix)
        second, seat2 = self.connect("FairUseSecond" + suffix)
        public, created = self.create_distribution([first, second])
        created.raise_for_status()
        fixed, created = self.create(first)
        created.raise_for_status()
        users = [f"fair-use-{letter}-{uuid.uuid4().hex}" for letter in "abc"]
        for user in users:
            self.client.post("/user/new", json={"user_id": user, "user_role": "internal_user"}).raise_for_status()

        def team(members):
            response = self.client.post("/team/new", json={"team_alias": uuid.uuid4().hex,
                "members_with_roles": [{"user_id": u, "role": "user"} for u in members]})
            response.raise_for_status()
            return response.json()["team_id"]

        team_a, team_b = team([users[0], users[2]]), team([users[1]])
        keys = []
        for user, group in zip(users, [team_a, team_b, team_a]):
            response = self.client.post("/key/generate", json={"user_id": user, "team_id": group, "models": [public, fixed]})
            response.raise_for_status()
            keys.append({"Authorization": "Bearer " + response.json()["key"]})
        a, b, c = keys
        for account, teams in [(first, [team_a]), (second, [team_a, team_b])]:
            self.client.put("/chatgpt/api/accounts/" + account["account"] + "/access",
                            json={"teams": teams, "users": []}).raise_for_status()

        def call(key, model, **extra):
            return self.client.post("/v1/chat/completions", headers=key, json={"model": model,
                "messages": [{"role": "user", "content": uuid.uuid4().hex}], **extra})

        now = int(time.time())
        windows = {"allowed": True, "primary_window": {"limit_window_seconds": 18000, "used_percent": 90,
                                                       "reset_at": now + 1800},
                   "secondary_window": {"limit_window_seconds": 604800, "used_percent": 10, "reset_at": now + 86400}}
        self.control(quota_by_identity={seat1: windows, seat2: windows})
        self.addCleanup(self.control, quota_by_identity={})
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            response = self.client.get("/chatgpt/fair-use/me", headers=a)
            response.raise_for_status()
            observed = [r for r in response.json()["accounts"] if r["display_name"] in (first["display_name"], second["display_name"])]
            if len(observed) == 2 and all(r["quota_state"] == "current" and r["windows"][0]["used_percent"] == 90 for r in observed):
                break
            time.sleep(1)
        else:
            self.fail("Fair-use refresher did not observe the fixture windows")

        for _ in range(3):
            call(a, fixed).raise_for_status()
        call(c, fixed).raise_for_status()
        denied = call(b, fixed)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["type"], "subscription_access_denied")
        self.assertNotIn(fixed, {m["id"] for m in self.client.get("/v1/models", headers=b).json()["data"]})
        self.control(clear=True)
        call(b, public).raise_for_status()
        # A has consumed 75% of recorded tokens on seat 1. Seat 2 has one light user.
        for _ in range(2):
            result = call(a, public)
            result.raise_for_status()
            self.assertIn("x-litellm-fair-use-reset", result.headers)
        blocked = call(a, public, num_retries=4)
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(blocked.json()["error"]["type"], "fair_use_limit")
        self.assertGreater(int(blocked.headers["Retry-After"]), 0)
        self.assertEqual(self.control()["seen"], [seat2, seat2, seat2])
        with self.client.stream("POST", "/v1/chat/completions", headers=c, json={
                "model": public, "stream": True, "messages": [{"role": "user", "content": "light"}]}) as streamed:
            self.assertEqual(streamed.status_code, 200)
            self.assertIn("x-litellm-fair-use-share", streamed.headers)
            self.assertIn("x-litellm-fair-use-allowance", streamed.headers)
            self.assertIn(b"[DONE]", streamed.read())


for _name in models.SubscriptionModelTests.__dict__:
    if _name.startswith("test_"):
        setattr(SubscriptionFairUseTests, _name, None)
