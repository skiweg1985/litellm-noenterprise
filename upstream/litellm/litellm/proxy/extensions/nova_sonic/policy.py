"""What a client may send in a Nova Sonic session.

Clients speak Bedrock's own event format for InvokeModelWithBidirectionalStream:
each WebSocket text frame holds one event as {"event": {<name>: {...}}}. The
proxy forwards a fixed set of input events and checks the tool configuration,
so that every tool runs on the client and its work goes through the proxy.
"""

import json

PATHS = ("/v1/sonic/sessions", "/sonic/sessions")

# The route stores the session context here; the Bedrock realtime handler
# reads it. Only the server writes the ASGI scope, a client cannot set it.
SCOPE_KEY = "litellm.nova_sonic"

MAX_FRAME_BYTES = 256 * 1024
# After the closing events Bedrock reports the final usage and ends the
# stream. Wait this long for it before closing anyway.
CLOSE_GRACE_SECONDS = 5

CLIENT_EVENTS = frozenset(
    {
        "sessionStart",
        "promptStart",
        "contentStart",
        "audioInput",
        "textInput",
        "toolResult",
        "contentEnd",
        "promptEnd",
        "sessionEnd",
    }
)

TOOL_CONFIGURATION_FIELDS = frozenset({"tools", "toolChoice"})
TOOL_SPEC_FIELDS = frozenset({"name", "description", "inputSchema"})
TOOL_CHOICES = frozenset({"auto", "any", "tool"})


class SonicError(Exception):
    """A rule violation. The message goes to the client, so no secrets in it.

    A fatal error ends the session; otherwise the event is answered with an
    error frame and dropped.
    """

    def __init__(self, message, code=1008, fatal=True):
        super().__init__(message)
        self.code = code
        self.fatal = fatal


def _reject_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        # Two fields with the same name could be read differently by Bedrock.
        raise SonicError("Duplicate fields in event.", 1007)
    return dict(pairs)


def parse_frame(text):
    """Return (name, body) of the one event in a client frame."""
    if text is None:
        raise SonicError("Only JSON text frames are supported.", 1003)
    if len(text.encode()) > MAX_FRAME_BYTES:
        raise SonicError("Frame exceeds 256 KiB.", 1009)
    try:
        frame = json.loads(text, object_pairs_hook=_reject_duplicates)
    except (ValueError, RecursionError):
        raise SonicError("Invalid JSON event.", 1007) from None
    event = frame.get("event") if isinstance(frame, dict) and len(frame) == 1 else None
    if not isinstance(event, dict) or len(event) != 1:
        raise SonicError('A frame must hold one event as {"event": {<name>: {...}}}.', 1007)
    ((name, body),) = event.items()
    if not isinstance(body, dict):
        raise SonicError("An event body must be an object.", 1007)
    return name, body


def _check_tools(configuration):
    if configuration is None:
        return
    if not isinstance(configuration, dict) or not configuration.keys() <= TOOL_CONFIGURATION_FIELDS:
        raise SonicError("toolConfiguration may only hold tools and toolChoice.", fatal=False)
    tools = configuration.get("tools", [])
    if not isinstance(tools, list):
        raise SonicError("toolConfiguration.tools must be a list.", fatal=False)
    for tool in tools:
        spec = tool.get("toolSpec") if isinstance(tool, dict) and len(tool) == 1 else None
        if not isinstance(spec, dict) or not spec.keys() <= TOOL_SPEC_FIELDS:
            raise SonicError(
                "Only client tools are supported: each tool is a toolSpec with name, "
                "description and inputSchema.",
                fatal=False,
            )
        if not isinstance(spec.get("name"), str) or not spec["name"]:
            raise SonicError("Each toolSpec needs a name.", fatal=False)
    choice = configuration.get("toolChoice")
    if choice is not None and (
        not isinstance(choice, dict) or len(choice) != 1 or not choice.keys() <= TOOL_CHOICES
    ):
        raise SonicError("toolChoice must be auto, any or tool.", fatal=False)


def check_client_event(name, body):
    if name not in CLIENT_EVENTS:
        raise SonicError(f"Event {name[:64]} is not supported.", fatal=False)
    if name == "promptStart":
        _check_tools(body.get("toolConfiguration"))


def encode(name, body):
    return json.dumps({"event": {name: body}})


def query_model(query_params):
    model = query_params.get("model")
    if not isinstance(model, str) or not model:
        raise SonicError("The model query parameter is required.")
    return model


def error_frame(message, kind="invalid_request_error"):
    return json.dumps({"error": {"type": kind, "message": message}})
