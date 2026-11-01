"""Owned minimal MCP service for identity/session isolation acceptance."""

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

server = MCPServer("identity-fixture")


@server.tool()
def echo(value: str) -> str:
    return value


server.run(
    transport="streamable-http",
    host="0.0.0.0",
    port=8090,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
