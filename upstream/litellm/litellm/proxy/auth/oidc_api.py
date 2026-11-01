"""Provider access-token admission, distinct from browser and upstream MCP grants."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Literal
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import HTTPException, Request
from litellm.proxy._types import LiteLLMRoutes, UserAPIKeyAuth
from litellm.proxy.auth import oidc_identity as identity
from litellm.proxy.auth.auth_utils import get_request_route
from litellm.proxy.auth.route_checks import RouteChecks
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class APIScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, pattern=r'^[^\s"\\]+$')
    token_value: str = Field(min_length=1, pattern=r"^\S+$")


class APISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: str
    audience: str = Field(min_length=1)
    scope_claim: str = "scope"
    scope_format: Literal["space_delimited", "array"] = "space_delimited"
    scopes: list[APIScope] = Field(min_length=1)
    token_types: list[Literal["at+jwt", "application/at+jwt", "JWT"]] = Field(
        default_factory=lambda: ["at+jwt"]
    )
    required_claims: list[identity.ClaimRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_resource(self):
        parsed = urlparse(self.resource)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API resource must be an absolute URL without credentials, query, or fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("API resource must be the public proxy origin")
        if not self.token_types or len({scope.name for scope in self.scopes}) != len(self.scopes):
            raise ValueError("API token types must be nonempty and scope names unique")
        self.resource = self.resource.rstrip("/")
        return self


def settings() -> APISettings | None:
    from litellm.proxy.proxy_server import general_settings

    from litellm.proxy.auth.oidc_settings import current_snapshot
    snapshot = current_snapshot.get()
    if snapshot is not None:
        if snapshot.error is not None:
            raise snapshot.error
        return snapshot.api
    value = general_settings.get("oidc_api")
    if value is None:
        return None
    try:
        config = APISettings.model_validate(value)
        browser = identity.require_settings()
        if config.audience == browser.client_id and "JWT" in config.token_types:
            raise ValueError(
                "Generic JWT access tokens require an audience distinct from the browser client ID"
            )
        if urlparse(config.resource).scheme != "https" and not (
            browser.allow_insecure_http and urlparse(config.resource).scheme == "http"
        ):
            raise ValueError("API resource requires HTTPS")
        return config
    except (ValidationError, ValueError):
        raise HTTPException(503, "OIDC API configuration is invalid") from None


def protected_resource_metadata(*, mcp: bool = False) -> dict | None:
    config = settings()
    if config is None:
        return None
    return {
        "resource": config.resource + ("/mcp" if mcp else ""),
        "authorization_servers": [identity.require_settings().issuer],
        "scopes_supported": [scope.name for scope in config.scopes],
        "bearer_methods_supported": ["header"],
    }


def challenge(config: APISettings, *, mcp: bool = False, error: str | None = None) -> dict[str, str]:
    url = config.resource + "/.well-known/oauth-protected-resource" + ("/mcp" if mcp else "")
    attributes = [f'resource_metadata="{url}"']
    if error:
        attributes.append(f'error="{error}"')
    attributes.append('scope="' + " ".join(scope.name for scope in config.scopes) + '"')
    return {"WWW-Authenticate": "Bearer " + ", ".join(attributes)}


async def verify_access_token(config: APISettings, encoded: str) -> dict:
    browser = identity.require_settings()
    try:
        header = jwt.get_unverified_header(encoded)
        algorithm = header.get("alg")
        if algorithm not in ("RS256", "ES256") or header.get("typ") not in config.token_types:
            raise ValueError("unsupported access-token header")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            metadata = await identity.discovery(browser, client)
            key = await identity.load_signing_key(client, metadata["jwks_uri"], header, algorithm)
            claims = jwt.decode(
                encoded,
                key.key,
                algorithms=[algorithm],
                audience=config.audience,
                issuer=browser.issuer,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
        # Generic JWT providers need a distinct API audience and delegated scopes;
        # strict access-token types may share the browser audience.
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if header["typ"] == "JWT" and browser.client_id in audiences:
            raise ValueError("generic JWT also targets the browser client")
        if "nonce" in claims or claims.get("token_use", "access") != "access":
            raise ValueError("not a resource access token")
        if claims.get("idtyp") == "app":
            raise ValueError("a user access token is required")
        return claims
    except HTTPException:
        raise
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid provider access token") from None
    except httpx.HTTPError:
        raise HTTPException(503, "Provider verification metadata is unavailable") from None


def authorize_api_route(request: Request, role: str) -> None:
    path = get_request_route(request).rstrip("/")
    if path in (
        "/models",
        "/v1/models",
        *LiteLLMRoutes.model_info_routes.value,
    ) or RouteChecks.is_llm_api_route(path):
        return
    identity.authorize_browser_route(request, role)


async def authenticate_provider(request: Request, api_key: str | None) -> UserAPIKeyAuth | None:
    if not api_key or api_key.startswith("sk-") or api_key.count(".") != 2:
        return None
    from litellm.proxy.proxy_server import master_key

    if isinstance(master_key, str) and secrets.compare_digest(api_key.encode(), master_key.encode()):
        return None
    config = settings()
    if config is None:
        return None
    mcp = get_request_route(request).rstrip("/") == "/mcp"
    try:
        claims = await verify_access_token(config, api_key)
        browser = identity.require_settings()
        scope_values = identity.claim_value(claims, config.scope_claim)
        if config.scope_format == "space_delimited":
            scope_values = scope_values.split() if isinstance(scope_values, str) else []
        if not isinstance(scope_values, list) or not all(
            scope.token_value in scope_values for scope in config.scopes
        ):
            raise HTTPException(
                403,
                "Required API scope is missing",
                headers=challenge(config, mcp=mcp, error="insufficient_scope"),
            )
        if not browser.access_rule.matches(claims) or not all(
            rule.matches(claims) for rule in config.required_claims
        ):
            raise HTTPException(403, "Identity does not satisfy the API access rules")
        user_id = identity.user_id_for_claims(browser, claims)
        role = browser.role_for(claims)
        authorize_api_route(request, role)
        # API tokens have their own grant context. Never replace the browser's
        # signed snapshot or persistent role with a possibly narrower API grant.
        repository = identity.user_repository()
        await repository.table.upsert(
            where={"user_id": user_id},
            data={
                "create": {
                    "user_id": user_id,
                    "sso_user_id": user_id,
                    "user_role": browser.default_role,
                    "models": [],
                },
                "update": {},
            },
        )
        user = await repository.find_by_id(user_id)
        request.state.oidc_claims = browser.retained_policy_claims(claims)
        request.state.oidc_user_id = user_id
        request.state.oidc_api = True
        auth = UserAPIKeyAuth(
            user_id=user_id,
            user_role=role,
            user_email=user.user_email,
            org_id=user.organization_id,
            api_key="oidc-access:" + hashlib.sha256(api_key.encode()).hexdigest(),
            models=list(user.models or []),
        )
        auth.requires_fresh_policy = True
        auth.oidc_identity_source = "provider"
        auth.oidc_verified_claims = request.state.oidc_claims
        from litellm.proxy._experimental.mcp_server.oidc_policy import attach_user_permissions

        await attach_user_permissions(auth, user)
        return auth
    except HTTPException as exc:
        if exc.status_code == 401:
            exc.headers = challenge(config, mcp=mcp, error="invalid_token")
        raise


def require_personal_identity(request: Request) -> None:
    """Gate personal credential routes only; native MCP administration is unchanged."""
    path = get_request_route(request).rstrip("/")
    personal = path == "/v1/mcp/user-credentials" or path == "/v1/mcp/server/oauth/session"
    personal = personal or bool(
        re.fullmatch(r"/v1/mcp/server/[^/]+/(?:oauth-user-credential(?:/status)?|user-credential)", path)
    )
    personal = personal or bool(
        re.fullmatch(r"/v1/mcp/server/oauth/[^/]+/(?:authorize|register|token)", path)
    )
    if personal and settings() is not None:
        raise HTTPException(
            403, "Personal MCP connections require a provider user token or OIDC browser session"
        )


def require_personal_mcp_subject(auth: UserAPIKeyAuth | None) -> None:
    """Native keys cannot select a user's persisted upstream credential."""
    if settings() is not None and (auth is None or auth.oidc_identity_source != "provider"):
        raise HTTPException(
            403, "Personal MCP tool use requires a provider user access token"
        )
