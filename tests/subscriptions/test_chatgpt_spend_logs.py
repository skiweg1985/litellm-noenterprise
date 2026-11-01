import asyncio
import sys
import unittest
from pathlib import Path

from litellm.proxy.extensions.chatgpt_subscriptions.spend_logs import SubscriptionInModelName

DEPLOYMENTS = [
    {"model_name": "gpt-6-sol-sub", "id": "dep-default", "account": "default"},
    {"model_name": "gpt-6-sol-sub", "id": "dep-account-a", "account": "account-a"},
]


def entry(model="chatgpt/gpt-6-sol", model_id="dep-account-a", provider="chatgpt"):
    return {
        "custom_llm_provider": provider,
        "standard_logging_object": {"model": model, "model_id": model_id, "custom_llm_provider": provider},
    }


class SpendLogTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0

        def deployments():
            self.calls += 1
            return DEPLOYMENTS

        self.hook = SubscriptionInModelName(deployments)

    def run_hook(self, kwargs):
        result = object()
        returned, same = asyncio.run(self.hook.async_logging_hook(kwargs, result, "acompletion"))
        self.assertIs(same, result)
        return returned["standard_logging_object"]["model"]

    def test_names_the_subscription_between_provider_and_model(self):
        self.assertEqual(self.run_hook(entry()), "chatgpt/account-a/gpt-6-sol")
        self.assertEqual(self.run_hook(entry(model_id="dep-default")), "chatgpt/default/gpt-6-sol")

    def test_sync_hook_and_repeated_calls(self):
        kwargs = entry()
        self.hook.logging_hook(kwargs, None, "completion")
        self.hook.logging_hook(kwargs, None, "completion")
        asyncio.run(self.hook.async_logging_hook(kwargs, None, "acompletion"))
        self.assertEqual(kwargs["standard_logging_object"]["model"], "chatgpt/account-a/gpt-6-sol")
        self.assertEqual(self.calls, 1)

    def test_leaves_other_entries_alone(self):
        for kwargs in (
            entry(model="azure/gpt-6-sol", provider="azure"),
            entry(model_id="dep-unknown"),
            entry(model_id=None),
            {"custom_llm_provider": "chatgpt"},
            {"custom_llm_provider": "chatgpt", "standard_logging_object": None},
        ):
            before = repr(kwargs)
            self.hook.logging_hook(kwargs, None, "completion")
            self.assertEqual(repr(kwargs), before)
        self.assertEqual(self.calls, 1)  # only the unknown deployment id needs a lookup

    def test_lookup_errors_never_break_logging(self):
        def broken():
            raise RuntimeError("router gone")

        kwargs = entry()
        SubscriptionInModelName(broken).logging_hook(kwargs, None, "completion")
        self.assertEqual(kwargs["standard_logging_object"]["model"], "chatgpt/gpt-6-sol")


if __name__ == "__main__":
    unittest.main()
