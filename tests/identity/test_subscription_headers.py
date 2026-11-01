"""Administrator-selected credentials cannot be replaced by request headers."""

import test_subscription_models as model_tests


class SubscriptionHeaderTests(model_tests.SubscriptionModelTests):
    def test_extra_headers_cannot_replace_the_selected_subscription(self):
        account, identity = self.connect("PinnedHeaders")
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        for endpoint in ("chat/completions", "responses"):
            body = {
                "model": name,
                "extra_headers": {
                    "cHaTgPt-AcCoUnT-Id": "unassigned-synthetic",
                    "AUTHORIZATION": "Bearer caller-synthetic",
                },
            }
            body.update(
                {"input": "Header synthetic"}
                if endpoint == "responses"
                else {"messages": [{"role": "user", "content": "Header synthetic"}]}
            )
            response = self.client.post(
                "/v1/" + endpoint,
                json=body,
                headers={
                    "ChatGPT-Account-Id": "forwarded-unassigned",
                    "X-Subscription-Probe": "forwarded-synthetic",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.control()["seen"], [identity, identity])
        self.assertTrue(
            all(row["credential_matches"] for row in self.control()["requests"])
        )
        self.assertTrue(
            all(row["forwarded_probe"] for row in self.control()["requests"])
        )


for _name in model_tests.SubscriptionModelTests.__dict__:
    if _name.startswith("test_"):
        setattr(SubscriptionHeaderTests, _name, None)
