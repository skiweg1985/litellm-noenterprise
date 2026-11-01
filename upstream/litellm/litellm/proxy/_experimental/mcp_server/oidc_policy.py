"""Verified OIDC claim grants and ceilings for personal MCP access."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from litellm.proxy._types import MCPOAuthUserCredentialStatus, UserAPIKeyAuth
from litellm.proxy.auth.oidc_identity import ClaimRule, settings

if TYPE_CHECKING:
    from litellm.models.user import LiteLLM_UserTable
    from litellm.proxy._experimental.mcp_server.db import OAuthCredentialPayload
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


async def credential_status(
    user_id: str, server: MCPServer
) -> MCPOAuthUserCredentialStatus:
    from litellm.proxy._experimental.mcp_server.db import (
        get_user_oauth_credential,
        is_oauth_credential_expired,
    )
    from litellm.proxy.proxy_server import prisma_client

    if server.needs_user_oauth_token and not server.client_id:
        return MCPOAuthUserCredentialStatus(
            server_id=server.server_id,
            has_credential=False,
            connection_mode="managed",
            connection_status="unavailable",
            configuration_error="oauth_client_required",
        )
    cred = await get_user_oauth_credential(prisma_client, user_id, server.server_id)
    expired = bool(cred and is_oauth_credential_expired(cred))
    can_refresh = bool(
        cred and cred.get("refresh_token") and not cred.get("reconnect_required")
    )
    return MCPOAuthUserCredentialStatus(
        server_id=server.server_id,
        connection_mode="managed"
        if server.needs_user_oauth_token
        else "client_supplied",
        has_credential=bool(cred),
        expires_at=cred.get("expires_at") if cred else None,
        connected_at=cred.get("connected_at") if cred else None,
        is_expired=expired,
        can_refresh=can_refresh,
        connection_status="disconnected"
        if not cred
        else (
            "reconnect_required"
            if cred.get("reconnect_required") or (expired and not can_refresh)
            else "connected"
        ),
    )


async def revoke_upstream(
    server: MCPServer | None, credential: OAuthCredentialPayload | None
) -> Literal["revoked", "unsupported", "failed", "no_credential"]:
    """Attempt advertised revocation after local deletion; never undo local disconnect."""
    import httpx
    from urllib.parse import urlparse
    from litellm.proxy._experimental.mcp_server.mcp_server_manager import (
        global_mcp_server_manager,
    )
    from litellm.proxy._experimental.mcp_server.oauth_utils import (
        build_upstream_oauth2_token_request,
    )

    if not credential:
        return "no_credential"
    if server is None:
        return "unsupported"
    parsed = urlparse(server.effective_token_url or "")
    issuer = server.issuer or (
        f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else None
    )
    if not issuer:
        return "unsupported"
    try:
        metadata = (
            await global_mcp_server_manager._fetch_single_authorization_server_metadata(
                issuer, issuer, require_issuer=issuer
            )
        )
        if metadata is None:
            return "failed"
        if not metadata.revocation_url:
            return "unsupported"
        target = urlparse(metadata.revocation_url)
        if (
            target.scheme not in ("https", "http")
            or target.username
            or target.password
            or not target.hostname
        ):
            return "failed"
        if target.scheme == "http" and urlparse(issuer).scheme != "http":
            return "failed"
        auth = build_upstream_oauth2_token_request(
            server,
            auth_method=server.token_endpoint_auth_method,
            client_id=server.client_id,
            client_secret=server.client_secret,
        )
        async with httpx.AsyncClient(
            timeout=5, trust_env=False, follow_redirects=False
        ) as client:
            for kind in ("refresh_token", "access_token"):
                token = credential.get(kind)
                if token:
                    response = await client.post(
                        metadata.revocation_url,
                        data={**auth.body, "token": token, "token_type_hint": kind},
                        headers=auth.headers,
                    )
                    response.raise_for_status()
        return "revoked"
    except Exception:
        return "failed"


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


def require_managed_client(server: MCPServer) -> None:
    if server.needs_user_oauth_token and not server.client_id:
        raise HTTPException(
            400,
            detail={
                "error": "oauth_client_required",
                "message": "Configure an OAuth client ID on this MCP server before connecting a managed account.",
            },
        )
