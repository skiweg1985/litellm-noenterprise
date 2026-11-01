import unittest
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import httpx
import test_subscription_models as base


class SubscriptionConversionTests(base.SubscriptionModelTests):
    def test_unmanaged_conversion_is_enforced_on_other_worker(self):
        self.assert_conversion()

    def test_converted_model_cannot_fall_back_to_unassigned_provider(self):
        self.assert_conversion(failing=True)

    def assert_conversion(self, failing=False):
        account, identity = self.connect("ReviewConverted")
        name = "conversion-" + uuid.uuid4().hex[:8]
        created = self.client.post(
            "/model/new",
            json={
                "model_name": name,
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": "synthetic",
                    "api_base": "http://chatgpt:8091",
                },
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        model_id = created.json()["model_id"]
        self.models.append(model_id)
        body = {
            "model": name,
            "messages": [{"role": "user", "content": "Synthetic conversion proof"}],
            "num_retries": 0,
        }

        def call(_):
            result = httpx.post(
                base.BASE + "/v1/chat/completions",
                headers={**base.AUTH, "Connection": "close"},
                json=body,
                trust_env=False,
                timeout=30,
            )
            content = (
                result.json().get("choices", [{}])[0].get("message", {}).get("content")
            )
            return result.status_code, content

        for _ in range(60):
            with ThreadPoolExecutor(max_workers=16) as pool:
                before = list(pool.map(call, range(32)))
            if all(
                code == 200 and content == "OLD_UNMANAGED" for code, content in before
            ):
                break
            time.sleep(1)
        else:
            self.fail("Could not observe unmanaged model convergence: " + repr(before))
        outside = None
        if failing:
            outside = "outside-" + uuid.uuid4().hex[:8]
            response = self.client.post(
                "/model/new",
                json={
                    "model_name": outside,
                    "litellm_params": {
                        "model": "openai/gpt-4o-mini",
                        "api_key": "synthetic",
                        "api_base": "http://chatgpt:8091",
                    },
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.models.append(response.json()["model_id"])
            body["model"] = outside
            for _ in range(60):
                with ThreadPoolExecutor(max_workers=16) as pool:
                    control = list(pool.map(call, range(32)))
                if all(
                    code == 200 and content == "OLD_UNMANAGED"
                    for code, content in control
                ):
                    break
                time.sleep(1)
            else:
                self.fail("Outside provider control did not become ready")
            body["model"] = name
        patched = self.client.patch(
            "/model/" + model_id + "/update",
            json={
                "litellm_params": {"model": "chatgpt/gpt-5.4"},
                "model_info": {
                    "subscription_assignment": {
                        "mode": "fixed",
                        "subscription_ids": [account["subscription_id"]],
                    }
                },
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        if failing:
            self.control(status=503, clear=True)
            body.update(fallbacks=[{name: [outside]}], disable_fallbacks=False)
            with ThreadPoolExecutor(max_workers=16) as pool:
                failed = list(pool.map(call, range(32)))
            self.assertTrue(
                all(
                    code >= 400 and content != "OLD_UNMANAGED"
                    for code, content in failed
                ),
                failed,
            )
            return
        self.control(clear=True)
        creator = self.client.post("/v1/chat/completions", json=body)
        self.assertEqual(creator.status_code, 200, creator.text)
        self.assertEqual(
            creator.json()["choices"][0]["message"]["content"], "ok", creator.text
        )
        with ThreadPoolExecutor(max_workers=16) as pool:
            after = list(pool.map(call, range(32)))
        print("UNMANAGED_CONVERSION_AFTER", after, flush=True)
        self.assertTrue(
            all(code == 200 and content == "ok" for code, content in after), after
        )


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        [
            SubscriptionConversionTests(
                "test_unmanaged_conversion_is_enforced_on_other_worker"
            ),
            SubscriptionConversionTests(
                "test_converted_model_cannot_fall_back_to_unassigned_provider"
            ),
        ]
    )
