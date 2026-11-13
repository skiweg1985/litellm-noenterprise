"""What a client may send in a GPT-Live session.

GPT-Live hands work it cannot do itself to a background model. With Responses
delegation Azure calls that model directly, past LiteLLM, its keys and its
spend tracking. Only client delegation is accepted: the app runs the background
work as a normal LiteLLM request and returns the result to the voice with
session.commentary.append.
"""

import json
import os

PATHS = ("/v1/live/sessions", "/live/sessions")

# The route stores the start frame here; the Azure realtime handler
# reads it. Only the server writes the ASGI scope, a client cannot set it.
SCOPE_KEY = "litellm.gpt_live"

MAX_FRAME_BYTES = 256 * 1024
START_TIMEOUT_SECONDS = 10
DEFAULT_MAX_SESSION_SECONDS = 1800
# After session.close Azure answers with session.closed, which carries the
# final usage. Wait this long for it before closing anyway.
CLOSE_GRACE_SECONDS = 5

CLIENT_EVENTS = frozenset(
    {
        "session.input_audio.append",
        "session.input_audio.mute",
        "session.input_audio.unmute",
        "session.instructions.append",
        "session.thinking.append",
        "session.commentary.append",
        "session.close",
    }
)


class LiveError(Exception):
    """A rule violation. The message goes to the client, so no secrets in it."""

    def __init__(self, message, code=1008):
        super().__init__(message)
        self.code = code


def max_session_seconds():
    value = os.environ.get("GPT_LIVE_MAX_SESSION_SECONDS", "")
    if not value:
        return DEFAULT_MAX_SESSION_SECONDS
    try:
        seconds = int(value)
    except ValueError:
        raise LiveError("GPT_LIVE_MAX_SESSION_SECONDS is not a number.", 1011) from None
    if seconds <= 0:
        raise LiveError("GPT_LIVE_MAX_SESSION_SECONDS must be positive.", 1011)
    return seconds


def _reject_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        # Two "type" or "session" fields could be read differently by Azure.
        raise LiveError("Duplicate fields in event.", 1007)
    return dict(pairs)


def parse_event(text):
    if text is None:
        raise LiveError("Only JSON text frames are supported.", 1003)
    if len(text.encode()) > MAX_FRAME_BYTES:
        raise LiveError("Frame exceeds 256 KiB.", 1009)
    try:
        event = json.loads(text, object_pairs_hook=_reject_duplicates)
    except (ValueError, RecursionError):
        raise LiveError("Invalid JSON event.", 1007) from None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise LiveError("Event without type.", 1007)
    return event


def start_model(event):
    """Check the first frame and return the model the client asks for."""
    if event["type"] != "session.start":
        raise LiveError("The first event must be session.start.")
    session = event.get("session")
    if not isinstance(session, dict):
        raise LiveError("session.start needs a session object.")
    model = session.get("model")
    if not isinstance(model, str) or not model:
        raise LiveError("session.model is required.")
    delegation = session.get("delegation")
    if delegation is not None:
        if not isinstance(delegation, dict) or delegation.get("type", "client") != "client":
            raise LiveError(
                "Only client delegation is supported. Run background work as a "
                "LiteLLM request and return it with session.commentary.append."
            )
    return model


def upstream_start(event, deployment):
    """The start frame for Azure: the LiteLLM model name becomes the deployment."""
    session = dict(event["session"])
    session["model"] = deployment
    return json.dumps({**event, "session": session})


def check_client_event(event):
    kind = event["type"]
    if kind == "session.start":
        raise LiveError("The session has already started.")
    if kind not in CLIENT_EVENTS:
        raise LiveError(f"Event {kind[:64]} is not supported.")
    return event


def live_url(api_base):
    """wss://<resource>/openai/v1/live/sessions from a model's api_base."""
    base = api_base.strip().rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif not base.startswith("wss://"):
        raise LiveError("api_base must start with https://.", 1011)
    return base + "/openai/v1/live/sessions"


def error_frame(message, kind="invalid_request_error"):
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}})
