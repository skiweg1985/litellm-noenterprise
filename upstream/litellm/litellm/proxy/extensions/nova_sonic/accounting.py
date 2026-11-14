"""Bill a Nova Sonic stream by the tokens Bedrock reports, through LiteLLM's logging.

Bedrock sends usageEvent during the stream. Its details.total block holds the
running totals of speech and text tokens in both directions, so the last
values count. Speech and text tokens have separate prices.
"""

import math
import time
from datetime import datetime

from .policy import SonicError

# Price field per counter in usageEvent.details.total.
PRICE_FIELDS = {
    ("input", "speechTokens"): "input_cost_per_audio_token",
    ("input", "textTokens"): "input_cost_per_token",
    ("output", "speechTokens"): "output_cost_per_audio_token",
    ("output", "textTokens"): "output_cost_per_token",
}


def _price(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def resolve_prices(litellm_params, deployment, model_info_lookup=None):
    """Token prices: deployment pricing first, then LiteLLM's price list."""
    params = litellm_params or {}
    infos = [params.get("model_info") or {}, (params.get("metadata") or {}).get("model_info") or {}]
    prices = {}
    for field in PRICE_FIELDS.values():
        for value in [params.get(field)] + [info.get(field) for info in infos]:
            price = _price(value)
            if price is not None:
                prices[field] = price
                break
    base = params.get("base_model") or next((i["base_model"] for i in infos if i.get("base_model")), None)
    candidates = [base.removeprefix("bedrock/")] if base else []
    candidates.append(deployment.removeprefix("bedrock/"))
    if model_info_lookup is None:
        import litellm

        def model_info_lookup(name):
            return litellm.get_model_info(model=name, custom_llm_provider="bedrock")

    for name in candidates:
        if len(prices) == len(PRICE_FIELDS):
            break
        try:
            info = model_info_lookup(name)
        except Exception:  # noqa: BLE001 - unknown model names raise
            continue
        for field in PRICE_FIELDS.values():
            price = _price(info.get(field))
            if field not in prices and price is not None:
                prices[field] = price
    if len(prices) != len(PRICE_FIELDS):
        raise SonicError(
            "No price for this Nova Sonic deployment. Set base_model to "
            "bedrock/amazon.nova-2-5-sonic or the four token prices in the model settings.",
            1011,
        )
    if any(price < 0 for price in prices.values()):
        raise SonicError("Nova Sonic token prices must not be negative.", 1011)
    return prices


class SessionAccounting:
    def __init__(self, logging_obj, alias, deployment, prices, clock=time.monotonic):
        self.logging_obj = logging_obj
        self.alias = alias
        self.deployment = deployment
        self.prices = prices
        self.clock = clock
        self.started = clock()
        self.start_time = datetime.now()  # noqa: DTZ005 - LiteLLM uses naive local timestamps
        self.accepted = False
        self.tokens = {key: 0 for key in PRICE_FIELDS}
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.usage_reported = False
        self.provider_session_id = None
        self.first_output_seconds = None
        self.tool_uses = 0
        self.provider_error = None
        self.end_reason = "client_close"
        self.finished = False

    def received(self, name, body):
        if name == "completionStart":
            self.provider_session_id = self.provider_session_id or body.get("sessionId")
        elif name == "toolUse":
            self.tool_uses += 1
        elif name == "audioOutput":
            if self.first_output_seconds is None:
                self.first_output_seconds = self.clock() - self.started
                self.logging_obj.update_completion_start_time(datetime.now())  # noqa: DTZ005
        elif name == "usageEvent":
            total = (body.get("details") or {}).get("total") or {}
            for direction, counter in PRICE_FIELDS:
                value = _count((total.get(direction) or {}).get(counter))
                self.tokens[(direction, counter)] = max(self.tokens[(direction, counter)], value)
            self.total_input_tokens = max(self.total_input_tokens, _count(body.get("totalInputTokens")))
            self.total_output_tokens = max(self.total_output_tokens, _count(body.get("totalOutputTokens")))
            self.usage_reported = True

    def cost(self):
        return sum(self.tokens[key] * self.prices[field] for key, field in PRICE_FIELDS.items())

    def summary(self):
        return {
            "input_speech_tokens": self.tokens[("input", "speechTokens")],
            "input_text_tokens": self.tokens[("input", "textTokens")],
            "output_speech_tokens": self.tokens[("output", "speechTokens")],
            "output_text_tokens": self.tokens[("output", "textTokens")],
            "usage_reported": self.usage_reported,
            "model_alias": self.alias,
            "deployment": self.deployment,
            "provider_session_id": self.provider_session_id,
            "tool_uses": self.tool_uses,
            "first_output_seconds": self.first_output_seconds,
            "session_duration_seconds": self.clock() - self.started,
            "end_reason": self.end_reason,
            "provider_error": self.provider_error,
        }

    async def finish(self, result_factory=None, success_key=None, failure_key=None):
        """Dispatch exactly one success or failure log for the stream."""
        if self.finished:
            return
        self.finished = True
        if success_key is None or failure_key is None:
            from litellm.constants import (
                REALTIME_SESSION_FAILURE_LOGGED_KEY,
                REALTIME_SESSION_SUCCESS_LOGGED_KEY,
            )

            success_key = success_key or REALTIME_SESSION_SUCCESS_LOGGED_KEY
            failure_key = failure_key or REALTIME_SESSION_FAILURE_LOGGED_KEY
        log = self.logging_obj
        summary = self.summary()
        params = log.model_call_details.setdefault("litellm_params", {})
        for field in ("metadata", "litellm_metadata"):
            metadata = params.get(field)
            if field == "metadata" and metadata is None:
                metadata = params["metadata"] = {}
            if isinstance(metadata, dict):
                metadata.setdefault("spend_logs_metadata", {})["nova_sonic"] = summary
        log.model_call_details["nova_sonic"] = summary
        end_time = datetime.now()  # noqa: DTZ005
        if not self.accepted and not self.usage_reported:
            # Bedrock refused the stream: nothing to bill.
            log.model_call_details[failure_key] = True
            error = SonicError(f"Nova Sonic stream not started ({self.end_reason}).", 1011)
            await log.dispatch_failure_handlers(error, str(error), prefer_async_handlers=True)
            return
        if self.first_output_seconds is None:
            log.update_completion_start_time(end_time)
        result = (result_factory or _logging_result)(self.total_input_tokens, self.total_output_tokens)
        result._hidden_params = {"response_cost": self.cost(), "nova_sonic": summary}
        log.model_call_details[success_key] = True
        await log.dispatch_success_handlers(
            result=result,
            start_time=self.start_time,
            end_time=end_time,
            prefer_async_handlers=True,
        )


def _logging_result(input_tokens, output_tokens):
    from litellm import Usage
    from litellm.types.utils import LiteLLMRealtimeStreamLoggingObject

    return LiteLLMRealtimeStreamLoggingObject(
        usage=Usage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        results=[],
    )
