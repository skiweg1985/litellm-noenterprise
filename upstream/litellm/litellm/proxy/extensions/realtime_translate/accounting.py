"""Bill a translation session by the input audio sent to Azure.

Azure reports no usage on the translation socket. The proxy counts the PCM16
mono 24 kHz bytes it forwarded (48,000 bytes per second) and multiplies them
by the per-second price. Sent silence counts; idle time and output do not.
Each session produces exactly one log entry through LiteLLM's callbacks,
except an empty one: accepted by Azure, no audio in either direction and a
normal client close. Clients send those to check reachability; they are not
logged unless LITELLM_TRANSLATE_LOG_EMPTY_SESSIONS is set.
"""

import base64
import math
import time
from datetime import datetime

from .policy import TranslationError, log_empty_sessions

BYTES_PER_SECOND = 48000
# Microsoft Global Standard list price, USD 2.04 per audio hour. Used when
# neither the deployment nor LiteLLM's price list names a price.
DEFAULT_INPUT_COST_PER_SECOND = 2.04 / 3600


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise TranslationError("input_cost_per_second must be a number.", 1011) from None
    if not math.isfinite(number) or number <= 0:
        raise TranslationError("input_cost_per_second must be positive.", 1011)
    return number


def resolve_rate(litellm_params, deployment, model_info_lookup=None):
    """Price per second: deployment pricing, then LiteLLM's price list, then the list price."""
    params = litellm_params or {}
    infos = [params.get("model_info") or {}, (params.get("metadata") or {}).get("model_info") or {}]
    for value in [params.get("input_cost_per_second")] + [i.get("input_cost_per_second") for i in infos]:
        rate = _number(value)
        if rate is not None:
            return rate
    base = params.get("base_model") or next((i["base_model"] for i in infos if i.get("base_model")), None)
    candidates = ([base] if base else []) + [f"azure/{deployment}"]
    if model_info_lookup is None:
        import litellm

        def model_info_lookup(name):
            return litellm.get_model_info(model=name, custom_llm_provider="azure")

    for name in candidates:
        try:
            value = model_info_lookup(name).get("input_cost_per_second")
        except Exception:  # noqa: BLE001 - unknown model names raise
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            return float(value)
    return DEFAULT_INPUT_COST_PER_SECOND


class SessionAccounting:
    def __init__(self, logging_obj, alias, deployment, rate, clock=time.monotonic):
        self.logging_obj = logging_obj
        self.alias = alias
        self.deployment = deployment
        self.rate = rate
        self.clock = clock
        self.started = clock()
        self.start_time = datetime.now()  # noqa: DTZ005 - LiteLLM uses naive local timestamps
        self.connected = False
        self.input_bytes = 0
        self.output_bytes = 0
        self.first_output_seconds = None
        self.provider_session_id = None
        self.upstream_status = None
        self.end_reason = "client_close"
        self.finished = False

    def sent(self, event):
        if event.get("type") == "session.input_audio_buffer.append":
            self.input_bytes += len(base64.b64decode(event["audio"], validate=True))

    def received(self, event):
        kind = event.get("type")
        if kind in ("session.created", "session.updated"):
            self.provider_session_id = self.provider_session_id or (event.get("session") or {}).get("id")
        if kind in ("session.output_audio.delta", "session.output_transcript.delta"):
            if self.first_output_seconds is None:
                self.first_output_seconds = self.clock() - self.started
                self.logging_obj.update_completion_start_time(datetime.now())  # noqa: DTZ005
        if kind == "session.output_audio.delta":
            try:
                self.output_bytes += len(base64.b64decode(event.get("delta") or "", validate=True))
            except (TypeError, ValueError):
                pass

    def is_empty(self):
        """Accepted by Azure, no audio either way, closed normally: a reachability probe."""
        return (
            self.connected
            and self.input_bytes == 0
            and self.output_bytes == 0
            and self.first_output_seconds is None
            and self.end_reason == "client_close"
        )

    def summary(self):
        seconds = self.input_bytes / BYTES_PER_SECOND
        return {
            "input_audio_seconds": seconds,
            "output_audio_seconds": self.output_bytes / BYTES_PER_SECOND,
            "usage_source": "proxy_pcm_audio",
            "input_cost_per_second": self.rate,
            "model_alias": self.alias,
            "deployment": self.deployment,
            "provider_session_id": self.provider_session_id,
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
        if self.is_empty() and not log_empty_sessions():
            # No log entry and no marker: the route releases the budget
            # reservation and the parallel-request slot itself.
            return
        summary = self.summary()
        cost = summary["input_audio_seconds"] * self.rate
        params = log.model_call_details.setdefault("litellm_params", {})
        for field in ("metadata", "litellm_metadata"):
            metadata = params.get(field)
            if field == "metadata" and metadata is None:
                metadata = params["metadata"] = {}
            if isinstance(metadata, dict):
                metadata.setdefault("spend_logs_metadata", {})["realtime_translation"] = summary
        log.model_call_details["realtime_translation"] = summary
        end_time = datetime.now()  # noqa: DTZ005
        if not self.connected:
            # Azure never accepted the session: nothing to bill.
            log.model_call_details[failure_key] = True
            error = TranslationError(f"Translation session not started ({self.end_reason}).", 1011)
            await log.dispatch_failure_handlers(error, str(error), prefer_async_handlers=True)
            return
        if self.first_output_seconds is None:
            log.update_completion_start_time(end_time)
        result = (result_factory or _logging_result)()
        result._hidden_params = {"response_cost": cost, "realtime_translation": summary}
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
