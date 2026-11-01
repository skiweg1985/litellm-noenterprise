"""Owned minimal MCP service for identity/session isolation acceptance."""

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

server = MCPServer("identity-fixture")


@server.tool()
def echo(value: str) -> str:
    return value


observed_headers = []
app = server.streamable_http_app(
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False), host="0.0.0.0"
)


async def observed(request):
    if request.method == "DELETE":
        observed_headers.clear()
    return JSONResponse(observed_headers)


app.add_route("/observed", observed, methods=["GET", "DELETE"])


class Capture:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            observed_headers.append(dict((key.decode(), value.decode()) for key, value in scope["headers"]))
        await self.inner(scope, receive, send)


uvicorn.run(Capture(app), host="0.0.0.0", port=8090)
