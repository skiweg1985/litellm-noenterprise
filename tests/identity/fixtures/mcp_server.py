"""Owned minimal MCP service for identity/session isolation acceptance."""

import asyncio
import base64
import hashlib
import secrets
import time
from contextvars import ContextVar
from urllib.parse import urlencode, parse_qs

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, RedirectResponse

server = MCPServer("identity-fixture")


@server.tool()
def echo(value: str) -> str:
    return value


actor_context = ContextVar("actor", default="anonymous")


@server.tool()
def whoami() -> str:
    return actor_context.get()


observed_headers = []
auth_codes = {}
access_tokens = {}
refresh_tokens = {}
oauth_actor = {"actor": "alice", "expires_in": 3600, "refresh": True}
oauth_events = []
refresh_started = asyncio.Event()
refresh_release = asyncio.Event()

app = server.streamable_http_app(
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="0.0.0.0",
)


async def observed(request):
    if request.method == "DELETE":
        observed_headers.clear()
    return JSONResponse(observed_headers)


app.add_route("/observed", observed, methods=["GET", "DELETE"])


async def control(request):
    oauth_actor.update(await request.json())
    if oauth_actor.get("pause_refresh"):
        refresh_started.clear()
        refresh_release.clear()
    return JSONResponse({"ok": True})


async def events(request):
    return JSONResponse(oauth_events)


async def metadata(request):
    return JSONResponse(
        {
            "issuer": "http://mcp-upstream:8090",
            "authorization_endpoint": "http://mcp-upstream:8090/oauth/authorize",
            "token_endpoint": "http://mcp-upstream:8090/oauth/token",
            "revocation_endpoint": "http://mcp-upstream:8090/oauth/revoke",
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        }
    )


async def authorize(request):
    params = dict(request.query_params)
    code = secrets.token_urlsafe(24)
    auth_codes[code] = {**params, **oauth_actor}
    return RedirectResponse(
        params["redirect_uri"]
        + "?"
        + urlencode({"state": params["state"], "code": code})
    )


async def token(request):
    values = parse_qs((await request.body()).decode())
    form = {key: vals[0] for key, vals in values.items()}
    if form.get("grant_type") == "authorization_code":
        grant = auth_codes.pop(form.get("code"), None)
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        if (
            not grant
            or grant.get("code_challenge") != challenge
            or grant["redirect_uri"] != form.get("redirect_uri")
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    else:
        if oauth_actor.get("refresh_unavailable"):
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        grant = refresh_tokens.pop(form.get("refresh_token"), None)
        if not grant or oauth_actor.get("fail_refresh"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        refresh_started.set()
        if oauth_actor.get("pause_refresh"):
            await asyncio.wait_for(refresh_release.wait(), timeout=30)
        grant = {**grant, "expires_in": 3600}
        oauth_events.append({"event": "refresh", "actor": grant["actor"]})
    access = "mcp-fixture-access-" + secrets.token_urlsafe(24)
    expiry = grant.get("expires_in", 3600)
    access_tokens[access] = {"actor": grant["actor"], "expires": time.time() + expiry}
    result = {"access_token": access, "token_type": "Bearer"}
    if not grant.get("omit_expires_in"):
        result["expires_in"] = expiry
    if grant.get("refresh", True):
        refresh = "mcp-fixture-refresh-" + secrets.token_urlsafe(24)
        refresh_tokens[refresh] = grant
        result["refresh_token"] = refresh
    return JSONResponse(result)


async def revoke(request):
    form = parse_qs((await request.body()).decode())
    value = form.get("token", [""])[0]
    grant = refresh_tokens.pop(value, None) or access_tokens.pop(value, None)
    oauth_events.append(
        {"event": "revoke", "actor": grant.get("actor") if grant else None}
    )
    return JSONResponse({})


async def revoke_actor(request):
    actor = (await request.json())["actor"]
    for store in (access_tokens, refresh_tokens):
        for key, value in list(store.items()):
            if value["actor"] == actor:
                del store[key]
    return JSONResponse({"revoked": True})


async def expire_access(request):
    actor = (await request.json())["actor"]
    for grant in access_tokens.values():
        if grant["actor"] == actor:
            grant["expires"] = 0
    return JSONResponse({"expired": True})


async def wait_refresh(request):
    await asyncio.wait_for(refresh_started.wait(), timeout=20)
    return JSONResponse({"started": True})


async def release_refresh(request):
    refresh_release.set()
    return JSONResponse({"released": True})


app.add_route("/control/revoke", revoke_actor, methods=["POST"])
app.add_route("/control/expire-access", expire_access, methods=["POST"])
app.add_route("/control/refresh-wait", wait_refresh)
app.add_route("/control/refresh-release", release_refresh, methods=["POST"])
app.add_route("/control/oauth", control, methods=["POST"])
app.add_route("/oauth/events", events)
app.add_route("/.well-known/oauth-authorization-server", metadata)
app.add_route("/oauth/authorize", authorize)
app.add_route("/oauth/token", token, methods=["POST"])
app.add_route("/oauth/revoke", revoke, methods=["POST"])


class Capture:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(
                (key.decode(), value.decode()) for key, value in scope["headers"]
            )
            observed_headers.append(headers)
            value = headers.get("authorization", "").removeprefix("Bearer ")
            if value.startswith("mcp-fixture-access-"):
                grant = access_tokens.get(value)
                if (
                    not grant
                    or grant["expires"] <= time.time()
                    or grant["actor"] == oauth_actor.get("reject_actor")
                ):
                    await JSONResponse({"error": "invalid_token"}, status_code=401)(
                        scope, receive, send
                    )
                    return
                actor_context.set(grant["actor"])
        await self.inner(scope, receive, send)


uvicorn.run(Capture(app), host="0.0.0.0", port=8090)
