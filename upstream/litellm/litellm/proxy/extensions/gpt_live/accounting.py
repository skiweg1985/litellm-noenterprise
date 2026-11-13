"""Bill a GPT-Live session by the seconds Azure reports, through LiteLLM's logging.

Azure sends the session duration as usage.seconds, about once a minute in
session.usage.updated and finally in session.closed. The value is a running
total. If the connection breaks before session.closed, the time since
session.started is billed when it is longer than the last reported value.
"""

import math
import time
from datetime import datetime

from .policy import LiveError


def _positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if math.isfinite(rate) else None


def resolve_rate(litellm_params, deployment, model_info_lookup=None):
    """Price per second: deployment pricing first, then LiteLLM's price list."""
    params = litellm_params or {}
    infos = [params.get("model_info") or {}, (params.get("metadata") or {}).get("model_info") or {}]
    for value in [params.get("input_cost_per_second")] + [i.get("input_cost_per_second") for i in infos]:
        rate = _positive(value)
        if rate is not None:
            if rate <= 0:
                raise LiveError("input_cost_per_second must be positive.", 1011)
            return rate
    base = params.get("base_model") or next((i["base_model"] for i in infos if i.get("base_model")), None)
    candidates = [base] if base else []
    candidates.append(f"azure/{deployment}")
    if model_info_lookup is None:
        import litellm

        def model_info_lookup(name):
            return litellm.get_model_info(model=name, custom_llm_provider="azure")

    for name in candidates:
        try:
            rate = _positive(model_info_lookup(name).get("input_cost_per_second"))
        except Exception:  # noqa: BLE001 - unknown model names raise
            continue
        if rate and rate > 0:
            return rate
    raise LiveError(
        "No price for this GPT-Live deployment. Set base_model to azure/gpt-live-1 "
        "or input_cost_per_second in the model settings.",
        1011,
    )


class SessionAccounting:
    def __init__(self, logging_obj, alias, deployment, rate, clock=time.monotonic):
        self.logging_obj = logging_obj
        self.alias = alias
        self.deployment = deployment
        self.rate = rate
        self.clock = clock
        self.started = clock()
        self.start_time = datetime.now()  # noqa: DTZ005 - LiteLLM uses naive local timestamps
        self.live_started = None
        self.reported_seconds = None
        self.closed_by_provider = False
        self.close_reason = None
        self.provider_session_id = None
        self.first_output_seconds = None
        self.delegations = 0
        self.upstream_status = None
        self.end_reason = "client_close"
        self.finished = False

    def received(self, event):
        kind = event.get("type")
        if kind == "session.started":
            self.live_started = self.clock()
            self.provider_session_id = (event.get("session") or {}).get("id")
        elif kind == "session.delegation.created":
            self.delegations += 1
        elif kind in ("session.output_audio.delta", "session.output_transcript.delta"):
            if self.first_output_seconds is None:
                self.first_output_seconds = self.clock() - self.started
                self.logging_obj.update_completion_start_time(datetime.now())  # noqa: DTZ005
        if kind in ("session.usage.updated", "session.closed"):
            seconds = _positive((event.get("usage") or {}).get("seconds"))
            if seconds is not None and seconds >= (self.reported_seconds or 0):
                self.reported_seconds = seconds
        if kind == "session.closed":
            self.closed_by_provider = True
            self.close_reason = event.get("reason")

    def billed_seconds(self):
        if self.closed_by_provider and self.reported_seconds is not None:
            return self.reported_seconds, "provider_session_closed"
        elapsed = self.clock() - self.live_started if self.live_started is not None else 0.0
        reported = self.reported_seconds or 0.0
        if reported >= elapsed and self.reported_seconds is not None:
            return reported, "provider_usage_updated"
        return elapsed, "elapsed_since_session_started"

    def summary(self):
        seconds, source = self.billed_seconds()
        return {
            "billed_seconds": seconds,
            "usage_source": source,
            "input_cost_per_second": self.rate,
            "model_alias": self.alias,
            "deployment": self.deployment,
            "provider_session_id": self.provider_session_id,
            "provider_close_reason": self.close_reason,
            "client_delegations": self.delegations,
            "first_output_seconds": self.first_output_seconds,
            "session_duration_seconds": self.clock() - self.started,
            "end_reason": self.end_reason,
            "upstream_status": self.upstream_status,
        }

    async def finish(self, result_factory=None, success_key=None, failure_key=None):
        """Dispatch exactly one success or failure log for the session."""
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
        cost = summary["billed_seconds"] * self.rate
        params = log.model_call_details.setdefault("litellm_params", {})
        for field in ("metadata", "litellm_metadata"):
            metadata = params.get(field)
            if field == "metadata" and metadata is None:
                metadata = params["metadata"] = {}
            if isinstance(metadata, dict):
                metadata.setdefault("spend_logs_metadata", {})["gpt_live"] = summary
        log.model_call_details["gpt_live"] = summary
        end_time = datetime.now()  # noqa: DTZ005
        if self.live_started is None:
            # Azure refused the session: nothing to bill, log it as a failure
            # like LiteLLM does for a refused realtime handshake.
            log.model_call_details[failure_key] = True
            error = LiveError(f"GPT-Live session not started ({self.end_reason}).", 1011)
            await log.dispatch_failure_handlers(error, str(error), prefer_async_handlers=True)
            return
        if self.first_output_seconds is None:
            log.update_completion_start_time(end_time)
        result = (result_factory or _logging_result)()
        result._hidden_params = {"response_cost": cost, "gpt_live": summary}
        log.model_call_details[success_key] = True
        await log.dispatch_success_handlers(
            result=result,
            start_time=self.start_time,
            end_time=end_time,
            prefer_async_handlers=True,
        )


def _logging_result():
    from litellm import Usage
    from litellm.types.utils import LiteLLMRealtimeStreamLoggingObject

    return LiteLLMRealtimeStreamLoggingObject(
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0), results=[]
    )
