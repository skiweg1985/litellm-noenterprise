import sys
import unittest
from pathlib import Path

from litellm.proxy.extensions.chatgpt_subscriptions.admin import resolve_deployments

CREDENTIALS = {
    "chatgpt-default": {"api_key": "chatgpt-account:default"},
    "chatgpt-account-a": {"api_key": "chatgpt-account:account-a"},
    "azure-main": {"api_key": "sk-azure", "api_base": "https://example"},
}


def deployment(name, id_, credential=None, api_key=None, model="chatgpt/gpt-6-sol"):
    params = {"model": model}
    if credential:
        params["litellm_credential_name"] = credential
    if api_key:
        params["api_key"] = api_key
    return {"model_name": name, "model_info": {"id": id_}, "litellm_params": params}


class ResolveTests(unittest.TestCase):
    def resolve(self, *deployments, known=("default", "account-a")):
        return resolve_deployments(list(deployments), CREDENTIALS.get, set(known))

    def test_subscription_comes_from_the_credential_or_api_key(self):
        found = self.resolve(
            deployment("sol-sub", "sol.default", credential="chatgpt-default"),
            deployment("sol-sub", "sol.account-a", credential="chatgpt-account-a"),
            deployment("sol-sub", "sol.direct", api_key="chatgpt-account:account-a"),
            deployment("sol-azure", "azure", credential="azure-main", model="azure/gpt-6-sol"),
        )
        self.assertEqual(
            [(d["id"], d["account"], d["credential"], d["problem"]) for d in found],
            [("sol.default", "default", "chatgpt-default", None),
             ("sol.account-a", "account-a", "chatgpt-account-a", None),
             ("sol.direct", "account-a", None, None)],
        )
        self.assertEqual([d["slug"] for d in found], ["gpt-6-sol"] * 3)

    def test_broken_deployments_are_reported(self):
        found = self.resolve(
            deployment("typo-sub", "t", credential="chatgpt-account-b"),
            deployment("bare-sub", "b"),
            deployment("foreign-sub", "f", credential="azure-main"),
            deployment("unknown-sub", "u", api_key="chatgpt-account:nobody"),
        )
        self.assertEqual(
            [(d["model_name"], d["account"], d["problem"]) for d in found],
            [("typo-sub", None, "credential_missing"),
             ("bare-sub", None, "no_subscription"),
             ("foreign-sub", None, "no_subscription"),
             ("unknown-sub", "nobody", "unknown_subscription")],
        )


if __name__ == "__main__":
    unittest.main()
