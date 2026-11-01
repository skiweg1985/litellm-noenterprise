"""Name the ChatGPT subscription in LiteLLM's request logs.

The "Model" column of the request logs shows the logged model, which is
"chatgpt/gpt-6-sol" for every subscription. LiteLLM passes each log entry to
the logging hook of every CustomLogger before any logging callback runs, the
one that writes the spend logs included. This hook puts the subscription
between provider and model, for example "chatgpt/account-a/gpt-6-sol". The UI
cuts the column after 15 characters and shows the full name as a tooltip.
"""

import logging

from . import admin

logger = logging.getLogger("chatgpt_subscriptions")
PREFIX = "chatgpt/"

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # unit tests without LiteLLM
    CustomLogger = object


class SubscriptionInModelName(CustomLogger):
    def __init__(self, deployments=None):
        super().__init__()
        self.deployments = deployments or admin.chatgpt_deployments

    def _rename(self, kwargs):
        entry = kwargs.get("standard_logging_object") if isinstance(kwargs, dict) else None
        if not isinstance(entry, dict):
            return
        model, model_id = entry.get("model"), entry.get("model_id")
        if not isinstance(model, str) or not model.startswith(PREFIX) or not model_id:
            return
        if "/" in model[len(PREFIX):]:
            return  # already named; ChatGPT model names have no slash
        try:
            account = next(
                (d["account"] for d in self.deployments() if d.get("id") == model_id), None
            )
        except Exception:  # noqa: BLE001 - logging must never fail because of this hook
            logger.exception("Cannot name the ChatGPT subscription in the request log.")
            return
        if account:
            entry["model"] = f"{PREFIX}{account}/{model[len(PREFIX):]}"

    def logging_hook(self, kwargs, result, call_type):
        self._rename(kwargs)
        return kwargs, result

    async def async_logging_hook(self, kwargs, result, call_type):
        self._rename(kwargs)
        return kwargs, result
