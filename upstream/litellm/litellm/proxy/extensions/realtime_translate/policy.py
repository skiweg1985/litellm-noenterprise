"""What a client may send in a translation session, and where it may go.

Azure translates continuously: the client appends audio, Azure streams
translated audio and text back. There are no turns, no response.create and no
tools. The protocol is therefore kept small: audio appends and the few session
settings a translation client needs. Everything else is rejected, including
anything that could change the target or carry credentials.
"""

import base64
import binascii
import json
import os
import re
from urllib.parse import urlencode, urlsplit

PATHS = ("/v1/realtime/translations",)

# The route stores the session context here; the Azure realtime handler reads
# it. Only the server writes the ASGI scope, a client cannot set it.
SCOPE_KEY = "litellm.realtime_translate"

MAX_MESSAGE_BYTES = 256 * 1024
DEFAULT_MAX_SESSION_SECONDS = 600
MAX_SESSION_SECONDS_LIMIT = 3600
TRANSLATION_DEPLOYMENT = "gpt-realtime-translate"


class TranslationError(Exception):
    """A rule violation. The message goes to the client, so no secrets in it."""

    def __init__(self, message, code=1008):
        super().__init__(message)
        self.code = code


def max_session_seconds():
    value = os.environ.get("LITELLM_TRANSLATE_MAX_SECONDS", "")
    if not value:
        return DEFAULT_MAX_SESSION_SECONDS
    try:
        seconds = int(value)
    except ValueError:
        raise TranslationError("LITELLM_TRANSLATE_MAX_SECONDS is not a number.", 1011) from None
    if not 1 <= seconds <= MAX_SESSION_SECONDS_LIMIT:
        raise TranslationError("LITELLM_TRANSLATE_MAX_SECONDS must be between 1 and 3600.", 1011)
    return seconds


def log_empty_sessions():
    """LITELLM_TRANSLATE_LOG_EMPTY_SESSIONS=true also logs sessions without audio."""
    return os.environ.get("LITELLM_TRANSLATE_LOG_EMPTY_SESSIONS", "").strip().lower() in ("1", "true", "yes")


def requested_model(query):
    """The model alias from ?model=..., the only query parameter allowed."""
    items = list(query.multi_items())
    if len(items) != 1 or items[0][0] != "model":
        raise TranslationError("Supply exactly one model parameter and no overrides.")
    model = items[0][1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", model):
        raise TranslationError("Translation model is not allowed.")
    return model


def is_translation_model(model, model_info=None):
    """True for Azure translation deployments, by name, base_model or flag."""
    info = model_info if isinstance(model_info, dict) else {}
    if info.get("realtime_translate") is True:
        return True
    for name in (model, info.get("base_model")):
        if isinstance(name, str):
            name = name.removeprefix("azure/")
            if name == TRANSLATION_DEPLOYMENT or name.startswith(TRANSLATION_DEPLOYMENT + "-20"):
                return True
    return False


def check_deployment(deployment):
    """A router deployment the translation route may use."""
    params = deployment.get("litellm_params") or {}
    model = str(params.get("model", ""))
    provider = params.get("custom_llm_provider")
    is_azure = provider == "azure" or (provider is None and model.startswith("azure/"))
    if (
        not is_azure
        or not is_translation_model(model, deployment.get("model_info"))
        or str(params.get("realtime_protocol", "")).upper() not in ("GA", "V1")
    ):
        raise TranslationError("Model is not configured for Azure GA translation.")


def azure_origin(value):
    """The Azure resource as https://host, without path, query or credentials."""
    if not isinstance(value, str):
        raise TranslationError("The translation deployment has no api_base.", 1011)
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise TranslationError("api_base must be the HTTPS origin of the Azure resource.", 1011)
    return value.rstrip("/")


def translation_url(api_base, model):
    return azure_origin(api_base).replace("https://", "wss://", 1) + (
        "/openai/v1/realtime/translations?" + urlencode({"model": model})
    )


def model_info_of(logging_obj):
    """model_info of the routed deployment, if the logging object carries it."""
    details = getattr(logging_obj, "model_call_details", None)
    params = details.get("litellm_params") if isinstance(details, dict) else None
    info = params.get("model_info") if isinstance(params, dict) else None
    return info if isinstance(info, dict) else None


def reject_outside_route(model, model_info=None):
    """Translation models only through the translation route.

    The ordinary realtime endpoints would reach Azure without the duration
    limit and the audio accounting of the translation route.
    """
    if is_translation_model(model, model_info):
        import litellm

        raise litellm.BadRequestError(
            message="Use /v1/realtime/translations for translation models.",
            model=str(model),
            llm_provider="azure",
        )


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise TranslationError("Duplicate event fields are not allowed.")
        result[name] = value
    return result


def client_event(raw):
    """Check one client frame and return it parsed; raise TranslationError otherwise."""
    if not isinstance(raw, str) or len(raw.encode()) > MAX_MESSAGE_BYTES:
        raise TranslationError("Send JSON text events of at most 256 KiB.")
    try:
        event = json.loads(raw, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError):
        raise TranslationError("Invalid translation event JSON.") from None
    if not isinstance(event, dict):
        raise TranslationError("Translation event must be an object.")
    if "event_id" in event and (not isinstance(event["event_id"], str) or len(event["event_id"]) > 128):
        raise TranslationError("Invalid event ID.")
    kind = event.get("type")
    if kind == "session.input_audio_buffer.append":
        if set(event) - {"type", "event_id", "audio"}:
            raise TranslationError("Audio event overrides are not allowed.")
        try:
            audio = base64.b64decode(event["audio"], validate=True)
            if not audio or len(audio) % 2:
                raise ValueError
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise TranslationError("Audio must be base64 PCM16 mono at 24 kHz.") from None
    elif kind == "session.update":
        _check_session_update(event)
    else:
        raise TranslationError("Unsupported translation client event.")
    return event


def _check_session_update(event):
    if set(event) - {"type", "event_id", "session"}:
        raise TranslationError("Session overrides are not allowed.")
    session = event.get("session")
    if not isinstance(session, dict) or set(session) != {"audio"}:
        raise TranslationError("Only translation audio settings may be updated.")
    audio = session["audio"]
    if not isinstance(audio, dict) or set(audio) - {"input", "output"}:
        raise TranslationError("Unsupported translation audio settings.")
    if "input" in audio:
        source = audio["input"]
        if not isinstance(source, dict) or set(source) - {"noise_reduction", "transcription"}:
            raise TranslationError("Unsupported translation input settings.")
        noise = source.get("noise_reduction")
        if noise is not None and (
            not isinstance(noise, dict) or set(noise) != {"type"} or noise["type"] not in ("near_field", "far_field")
        ):
            raise TranslationError("Use near_field, far_field or null for noise reduction.")
        transcription = source.get("transcription")
        if transcription is not None and (
            not isinstance(transcription, dict)
            or set(transcription) != {"model"}
            or not isinstance(transcription["model"], str)
            or not transcription["model"].strip()
            or len(transcription["model"]) > 256
        ):
            raise TranslationError("Supply a transcription model or null.")
    if "output" in audio:
        output = audio["output"]
        if (
            not isinstance(output, dict)
            or set(output) != {"language"}
            or not isinstance(output["language"], str)
            or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,8})?", output["language"])
        ):
            raise TranslationError("Supply a supported output language code.")


def error_frame(message):
    return json.dumps({"type": "error", "error": {"type": "translation_error", "message": message}})
