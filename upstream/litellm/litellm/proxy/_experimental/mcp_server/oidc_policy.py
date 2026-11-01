"""Verified OIDC claim grants and ceilings for personal MCP access."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from fastapi import HTTPException
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.oidc_identity import ClaimRule, settings

if TYPE_CHECKING:
    from litellm.models.user import LiteLLM_UserTable
    from litellm.types.mcp_server.mcp_server_manager import MCPServer


def server_rule(auth: UserAPIKeyAuth | None, server: MCPServer) -> ClaimRule | None:
    config = settings()
    if (
        config is None
        or auth is None
        or auth.oidc_identity_source not in ("browser", "provider")
    ):
        return None
    for identifier in (server.server_id, server.alias, server.name):
        if identifier in config.mcp_access_rules:
            return config.mcp_access_rules[identifier]
    return None


def require_server_access(auth: UserAPIKeyAuth | None, server: MCPServer) -> None:
    rule = server_rule(auth, server)
    if (
        rule is not None
        and auth is not None
        and not rule.matches(auth.oidc_verified_claims)
    ):
        raise HTTPException(
            403, "Your identity does not permit access to this MCP server"
        )


def apply_server_rules(
    auth: UserAPIKeyAuth | None, servers: Iterable[MCPServer], allowed: Iterable[str]
) -> list[str]:
    result = set(allowed)
    for server in servers:
        rule = server_rule(auth, server)
        if rule is not None and auth is not None:
            if rule.matches(auth.oidc_verified_claims):
                result.add(server.server_id)
            else:
                result.discard(server.server_id)
    return sorted(result)


async def attach_user_permissions(
    auth: UserAPIKeyAuth, user: LiteLLM_UserTable
) -> None:
    """Load the existing user's MCP tool settings without replacing token-local identity."""
    from litellm.proxy.auth.auth_checks import get_object_permission
    from litellm.proxy.proxy_server import prisma_client, user_api_key_cache

    if user.object_permission_id:
        auth.object_permission = await get_object_permission(
            user.object_permission_id,
            prisma_client,
            user_api_key_cache,
            check_db_only=True,
        )
        auth.object_permission_id = user.object_permission_id
