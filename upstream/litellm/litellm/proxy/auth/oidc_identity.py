"""Independent OIDC browser admission and shared claim policy.

Provider credentials stay on the server. The existing dashboard receives only a
non-secret marker, bound to a separate HttpOnly browser-session cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote_plus, urlencode, urlparse

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.repositories.user_repository import UserRepository
from litellm.repositories.verification_token_repository import (
    VerificationTokenRepository,
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

BROWSER_MARKER = "oidc-browser-session"
SESSION_COOKIE = "litellm_oidc_session"
FLOW_COOKIE = "litellm_oidc_flow"
METADATA_KEY = "noenterprise_oidc"
router = APIRouter(prefix="/oidc", tags=["OIDC identity"])


def claim_value(claims: dict[str, Any], path: str) -> Any:
    if path in claims:
        return claims[path]
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class ClaimRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(min_length=1)
    any_of: list[str] = Field(min_length=1)
    value_format: Literal["scalar_or_array", "space_delimited"] = "scalar_or_array"

    def matches(self, claims: dict[str, Any]) -> bool:
        value = claim_value(claims, self.claim)
        if self.value_format == "space_delimited" and isinstance(value, str):
            value = value.split()
        values = value if isinstance(value, list) else [value]
        return any(isinstance(item, str) and item in self.any_of for item in values)


class RoleMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: ClaimRule
    role: LitellmUserRoles


class OIDCSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issuer: str
    client_id: str = Field(min_length=1)
    client_secret_env: str = Field(min_length=1)
    redirect_uri: str
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    subject_claim: str = "sub"
    access_rule: ClaimRule
    role_mappings: list[RoleMapping] = Field(default_factory=list)
    default_role: Literal["internal_user_viewer", "customer"] = "internal_user_viewer"
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    session_secret_env: str = "LITELLM_OIDC_SESSION_SECRET"
    mcp_access_rules: dict[str, ClaimRule] = Field(default_factory=dict)
    retained_claims: list[str] = Field(default_factory=lambda: ["roles", "groups", "scp", "scope"])
    allow_insecure_http: bool = False
    _active_revision: int = PrivateAttr(default=0)
    _client_secret: str | None = PrivateAttr(default=None)
    _session_secret: str | None = PrivateAttr(default=None)

    def client_secret(self) -> str | None:
        return self._client_secret if self._client_secret is not None else os.environ.get(self.client_secret_env)

    def session_secret(self) -> str:
        return self._session_secret if self._session_secret is not None else os.environ.get(self.session_secret_env, "")

    @model_validator(mode="after")
    def validate_endpoints(self):
        for value in (self.issuer, self.redirect_uri):
            parsed = urlparse(value)
            if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise ValueError("OIDC URLs must be absolute URLs without credentials, query, or fragment")
            if parsed.scheme != "https" and not (self.allow_insecure_http and parsed.scheme == "http"):
                raise ValueError("OIDC endpoints require HTTPS")
        if "openid" not in self.scopes:
            raise ValueError("OIDC scopes must include openid")
        return self

    @property
    def origin(self) -> str:
        parsed = urlparse(self.redirect_uri)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def fingerprint(self) -> str:
        value = self.model_dump_json()
        if self._active_revision:
            value += "\0" + str(self._active_revision)
        return hashlib.sha256(value.encode()).hexdigest()

    def role_for(self, claims: dict[str, Any]) -> str:
        return next(
            (mapping.role.value for mapping in self.role_mappings if mapping.rule.matches(claims)), self.default_role
        )

    def retained_policy_claims(self, claims: dict[str, Any]) -> dict[str, Any]:
        paths = {*self.retained_claims, self.subject_claim, self.access_rule.claim}
        paths.update(mapping.rule.claim for mapping in self.role_mappings)
        paths.update(rule.claim for rule in self.mcp_access_rules.values())
        return {path: claim_value(claims, path) for path in sorted(paths) if claim_value(claims, path) is not None}


def settings() -> OIDCSettings | None:
    from litellm.proxy.proxy_server import general_settings

    from litellm.proxy.auth.oidc_settings import current_snapshot
    snapshot = current_snapshot.get()
    if snapshot is not None:
        if snapshot.error is not None:
            raise snapshot.error
        return snapshot.browser
    value = general_settings.get("oidc_identity")
    if value is None:
        return None
    try:
        return OIDCSettings.model_validate(value)
    except ValidationError:
        raise HTTPException(503, "OIDC configuration is invalid") from None


def validate_configuration() -> None:
    """Fail at startup rather than leaving the native unauthenticated dev fallback active."""
    from litellm.proxy.proxy_server import general_settings, master_key, prisma_client

    from litellm.proxy.auth.oidc_api import settings as api_settings

    config = settings()
    api_settings()
    if config is None:
        return
    if not master_key:
        raise RuntimeError("OIDC requires a master key; anonymous development mode is not supported")
    if general_settings.get("custom_auth"):
        raise RuntimeError("OIDC identity cannot be combined with a replacing custom_auth hook")
    if prisma_client is None:
        raise RuntimeError("OIDC requires the user database")
    if not config.client_secret():
        raise RuntimeError("OIDC client credential is unavailable")
    cipher(config, "session")


def require_settings() -> OIDCSettings:
    config = settings()
    if config is None:
        raise HTTPException(404, "OIDC is not configured")
    return config


def cipher(config: OIDCSettings, purpose: str) -> Fernet:
    secret = config.session_secret()
    if len(secret) < 32:
        raise HTTPException(503, "OIDC session secret must contain at least 32 characters")
    key = hashlib.sha256((purpose + "\0" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(config: OIDCSettings, purpose: str, value: dict[str, Any]) -> str:
    return cipher(config, purpose).encrypt(json.dumps(value).encode()).decode()


def unseal(config: OIDCSettings, purpose: str, value: str | None, ttl: int) -> dict[str, Any]:
    if not value:
        raise HTTPException(401, "OIDC browser session is required")
    try:
        return json.loads(cipher(config, purpose).decrypt(value.encode(), ttl=ttl))
    except (InvalidToken, ValueError, TypeError):
        raise HTTPException(401, "OIDC browser session is invalid or expired") from None


def set_cookie(response, config: OIDCSettings, name: str, value: str, ttl: int, *, httponly=True):
    response.set_cookie(
        name,
        value,
        max_age=ttl,
        secure=config.redirect_uri.startswith("https://"),
        httponly=httponly,
        samesite="lax",
        path="/",
    )


def user_id_for_claims(config: OIDCSettings, claims: dict[str, Any]) -> str:
    subject = claim_value(claims, config.subject_claim)
    if not isinstance(subject, str) or not subject:
        raise HTTPException(403, "Required stable identity claim is missing")
    return "oidc-" + hashlib.sha256((config.issuer + "\0" + subject).encode()).hexdigest()


def user_repository() -> UserRepository:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(503, "OIDC requires the user database")
    return UserRepository(prisma_client, use_writer=True)


def session_repository() -> VerificationTokenRepository:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(503, "OIDC requires the session database")
    return VerificationTokenRepository(prisma_client, use_writer=True)


def sign_identity_state(config: OIDCSettings, user_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Generic user metadata is editable through native keys; authenticate our reserved state."""
    value = {key: item for key, item in state.items() if key != "signature"}
    body = json.dumps([user_id, value], sort_keys=True, separators=(",", ":")).encode()
    secret = config.session_secret().encode()
    signature = hmac.new(secret, b"oidc-identity-state\0" + body, hashlib.sha256).hexdigest()
    return {**value, "signature": signature}


def verified_identity_state(config: OIDCSettings, user_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    state = metadata.get(METADATA_KEY)
    if not isinstance(state, dict) or not isinstance(state.get("signature"), str):
        return {}
    expected = sign_identity_state(config, user_id, state)["signature"]
    return state if secrets.compare_digest(state["signature"], expected) else {}


def session_prefix(user_id: str) -> str:
    # Native keys are SHA256 hex strings. This reserved namespace cannot be recreated
    # through key/generate. Bind revocation to the immutable ID, not editable row metadata.
    return "oidc-session:" + user_id + ":"


async def revoke_identity_sessions(user_id: str) -> None:
    await session_repository().table.delete_many(where={"token": {"startswith": session_prefix(user_id)}})


async def invalidate_user_auth(user_id: str) -> None:
    from litellm.proxy.common_utils.auth_cache_invalidation_pubsub import (
        evict_and_broadcast,
    )
    from litellm.proxy.proxy_server import user_api_key_cache

    rows = await session_repository().table.find_many(where={"user_id": user_id})
    await evict_and_broadcast(cache_keys=(user_id, *(row.token for row in rows)), user_api_key_cache=user_api_key_cache)


async def synchronize_account(config: OIDCSettings, claims: dict[str, Any]):
    """Apply the same admission policy for new and existing users, without email linking."""
    from litellm.proxy.auth.oidc_links import resolve_account, associated_user
    repository = user_repository()
    previous = await associated_user(config, claims)
    user_id = previous.user_id if previous else user_id_for_claims(config, claims)
    metadata = dict(previous.metadata or {}) if previous else {}
    old = verified_identity_state(config, user_id, metadata)
    if not config.access_rule.matches(claims):
        await revoke_identity_sessions(user_id)
        if previous:
            metadata[METADATA_KEY] = sign_identity_state(
                config, user_id, {**old, "active": False, "epoch": secrets.token_urlsafe(24)}
            )
            await repository.update(user_id, {"metadata": json.dumps(metadata)}, id_field="user_id")
        await invalidate_user_auth(user_id)
        raise HTTPException(403, "Identity does not satisfy the access rule")
    previous = await resolve_account(config, claims)
    user_id = previous.user_id
    metadata = dict(previous.metadata or {})
    old = verified_identity_state(config, user_id, metadata)
    role = config.role_for(claims)
    policy_claims = config.retained_policy_claims(claims)
    digest = hashlib.sha256(json.dumps([role, policy_claims, config.fingerprint], sort_keys=True).encode()).hexdigest()
    epoch = old.get("epoch") if old.get("active") and old.get("claims_digest") == digest else None
    if epoch is None:
        # A signature alone would allow replaying an older valid metadata snapshot.
        # Destroy its session handles before publishing the replacement policy.
        await revoke_identity_sessions(user_id)
    metadata[METADATA_KEY] = sign_identity_state(
        config,
        user_id,
        {
            "active": True,
            "epoch": epoch or secrets.token_urlsafe(24),
            "claims_digest": digest,
            "claims": policy_claims,
            "config": config.fingerprint,
        },
    )
    values = {"user_role": role, "metadata": json.dumps(metadata)}
    # Email is informational, never an account lookup/linking key.
    email = claims.get("email")
    if isinstance(email, str):
        values["user_email"] = email
    from litellm.proxy.auth.oidc_links import update_browser_account
    await update_browser_account(config, claims, user_id, values)
    await invalidate_user_auth(user_id)
    return await repository.find_by_id(user_id)


async def discovery(config: OIDCSettings, client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(config.issuer.rstrip("/") + "/.well-known/openid-configuration")
    response.raise_for_status()
    metadata = response.json()
    if metadata.get("issuer") != config.issuer:
        raise HTTPException(401, "OIDC discovery issuer mismatch")
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        endpoint = urlparse(metadata.get(name, ""))
        if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.fragment:
            raise HTTPException(503, "OIDC discovery endpoint is invalid")
        if endpoint.scheme != "https" and not (config.allow_insecure_http and endpoint.scheme == "http"):
            raise HTTPException(503, "OIDC discovery endpoints require HTTPS")
    return metadata


async def load_signing_key(
    client: httpx.AsyncClient, jwks_uri: str, header: dict[str, Any], algorithm: str
) -> jwt.PyJWK:
    response = await client.get(jwks_uri)
    response.raise_for_status()
    matches = [
        key
        for key in response.json()["keys"]
        if key.get("kid") == header.get("kid")
        and key.get("use", "sig") == "sig"
        and key.get("alg", algorithm) == algorithm
    ]
    if len(matches) != 1:
        raise ValueError("ambiguous or missing signing key")
    return jwt.PyJWK.from_dict(matches[0], algorithm=algorithm)


async def verify_id_token(
    config: OIDCSettings, client: httpx.AsyncClient, metadata: dict[str, Any], encoded: str, nonce: str
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(encoded)
        algorithm = header.get("alg")
        if algorithm not in ("RS256", "ES256") or algorithm not in metadata.get(
            "id_token_signing_alg_values_supported", ["RS256"]
        ):
            raise ValueError("unsupported signing algorithm")
        key = await load_signing_key(client, metadata["jwks_uri"], header, algorithm)
        claims = jwt.decode(
            encoded,
            key.key,
            algorithms=[algorithm],
            audience=config.client_id,
            issuer=config.issuer,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
        )
        if not isinstance(claims["nonce"], str) or not secrets.compare_digest(claims["nonce"], nonce):
            raise ValueError("nonce mismatch")
        audience = claims["aud"]
        if (isinstance(audience, list) and len(audience) > 1 and "azp" not in claims) or (
            "azp" in claims and claims["azp"] != config.client_id
        ):
            raise ValueError("authorized party mismatch")
        return claims
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "OIDC identity token is invalid") from None


@router.get("/login")
async def begin_login(request: Request):
    config = require_settings()
    secret = config.client_secret()
    if not secret:
        raise HTTPException(503, "OIDC client credential is unavailable")
    flow = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": secrets.token_urlsafe(48),
    }
    challenge = base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).decode().rstrip("=")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            metadata = await discovery(config, client)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "OIDC provider discovery is unavailable") from None
    query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(config.scopes),
            "state": flow["state"],
            "nonce": flow["nonce"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in metadata["authorization_endpoint"] else "?"
    response = RedirectResponse(metadata["authorization_endpoint"] + separator + query, status_code=302)
    set_cookie(response, config, FLOW_COOKIE, seal(config, "flow", flow), 600)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback")
async def complete_login(request: Request):
    config = require_settings()
    flow = unseal(config, "flow", request.cookies.get(FLOW_COOKIE), 600)
    state = request.query_params.get("state", "")
    if not secrets.compare_digest(state, flow["state"]):
        raise HTTPException(401, "OIDC state does not match this browser")
    code = request.query_params.get("code")
    if request.query_params.get("error") or not code:
        raise HTTPException(401, "OIDC authorization was not completed")
    secret = config.client_secret()
    if not secret:
        raise HTTPException(503, "OIDC client credential is unavailable")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            metadata = await discovery(config, client)
            data = {
                "grant_type": "authorization_code",
                "redirect_uri": config.redirect_uri,
                "code": code,
                "code_verifier": flow["verifier"],
            }
            methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
            auth = None
            if "client_secret_basic" in methods:
                auth = httpx.BasicAuth(quote_plus(config.client_id), quote_plus(secret))
            elif "client_secret_post" in methods:
                data.update(client_id=config.client_id, client_secret=secret)
            else:
                raise HTTPException(503, "OIDC provider requires an unsupported client authentication method")
            token = await client.post(metadata["token_endpoint"], data=data, auth=auth)
            token.raise_for_status()
            claims = await verify_id_token(config, client, metadata, token.json()["id_token"], flow["nonce"])
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(401, "OIDC token exchange failed") from None
    try:
        user = await synchronize_account(config, claims)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "linking_required":
            from fastapi.responses import HTMLResponse
            response = HTMLResponse('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Account linking</title><main><h1>Account linking requires administrator review.</h1><p>Try signing in again after an administrator has reviewed your request.</p></main></html>', status_code=403, headers={"Cache-Control": "no-store"})
            for cookie in (SESSION_COOKIE, FLOW_COOKIE, "token"):
                response.delete_cookie(cookie, path="/")
            return response
        raise
    state = user.metadata[METADATA_KEY]
    role = user.user_role
    response = RedirectResponse(
        "/ui/" if role in ("proxy_admin", "proxy_admin_viewer") else "/ui/connect/", status_code=303
    )
    session_id = secrets.token_urlsafe(32)
    session_hash = session_prefix(user.user_id) + hashlib.sha256(session_id.encode()).hexdigest()
    await session_repository().create(
        {
            "token": session_hash,
            "user_id": user.user_id,
            "team_id": UI_SESSION_TOKEN_TEAM_ID,
            "models": [],
            "blocked": True,
            "expires": datetime.now(UTC) + timedelta(seconds=config.session_ttl_seconds),
            "metadata": json.dumps({"noenterprise_oidc_session": True}),
        }
    )
    session = {"user_id": user.user_id, "epoch": state["epoch"], "config": config.fingerprint, "sid": session_id}
    set_cookie(response, config, SESSION_COOKIE, seal(config, "session", session), config.session_ttl_seconds)
    # The readable compatibility envelope contains a marker, never a provider token or usable API key.
    from litellm.proxy.proxy_server import master_key

    envelope = {
        "user_id": user.user_id,
        "key": BROWSER_MARKER,
        "user_email": user.user_email,
        "user_role": role,
        "login_method": "sso",
        "premium_user": False,
        "auth_header_name": "Authorization",
        "disabled_non_admin_personal_key_creation": True,
        "server_root_path": "",
        "password_reset_required": False,
        "exp": int(time.time()) + config.session_ttl_seconds,
    }
    set_cookie(
        response,
        config,
        "token",
        jwt.encode(envelope, master_key or config.session_secret(), algorithm="HS256"),
        config.session_ttl_seconds,
        httponly=False,
    )
    response.delete_cookie(FLOW_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


async def current_session(request: Request):
    config = require_settings()
    session = unseal(config, "session", request.cookies.get(SESSION_COOKIE), config.session_ttl_seconds)
    if session.get("config") != config.fingerprint:
        raise HTTPException(401, "OIDC configuration changed; sign in again")
    session_hash = (
        session_prefix(str(session.get("user_id", "")))
        + hashlib.sha256(str(session.get("sid", "")).encode()).hexdigest()
    )
    record = await session_repository().find_by_id(session_hash)
    if (
        record is None
        or record.user_id != session.get("user_id")
        or not (record.metadata or {}).get("noenterprise_oidc_session")
    ):
        raise HTTPException(401, "OIDC browser session has been revoked")
    if record.expires is None or record.expires.replace(tzinfo=record.expires.tzinfo or UTC) <= datetime.now(UTC):
        raise HTTPException(401, "OIDC browser session has expired")
    user = await user_repository().find_by_id(session.get("user_id", ""))
    from litellm.proxy.auth.oidc_links import association_active
    if not await association_active(config, user):
        raise HTTPException(401, "OIDC account association is no longer active")
    state = verified_identity_state(config, user.user_id, user.metadata or {}) if user else {}
    if (
        not state.get("active")
        or state.get("epoch") != session.get("epoch")
        or state.get("config") != config.fingerprint
    ):
        raise HTTPException(401, "OIDC session is no longer authorized")
    return config, user, {**state, "session_hash": session_hash}


def require_same_origin(request: Request, config: OIDCSettings):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("origin") != config.origin:
        raise HTTPException(403, "Browser session request requires a same-origin Origin header")


@router.get("/session")
async def session_info(request: Request):
    _, user, _ = await current_session(request)
    return JSONResponse(
        {
            "user_id": user.user_id,
            "role": user.user_role,
            "admin_access": user.user_role in ("proxy_admin", "proxy_admin_viewer"),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/logout")
async def logout(request: Request):
    config, user, state = await current_session(request)
    require_same_origin(request, config)
    await session_repository().table.delete_many(where={"token": state["session_hash"], "user_id": user.user_id})
    response = JSONResponse({"logged_out": True, "message": "Session revoked."}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie("token", path="/")
    return response


def is_personal_mcp_route(path: str, method: str) -> bool:
    """Allow the existing connection surface, without opening server administration."""
    path = path.rstrip("/")
    if re.fullmatch(r"/mcp(?:/[^/]+)?", path) or re.fullmatch(r"/[^/]+/mcp", path):
        return method in ("GET", "POST", "DELETE")
    if path == "/session/logout":
        return method == "POST"
    if path in ("/v1/mcp/server", "/v1/mcp/tools", "/v1/mcp/user-credentials"):
        return method == "GET"
    if path == "/v1/mcp/server/oauth/session":
        return method == "POST"
    if re.fullmatch(r"/v1/mcp/server/oauth/[^/]+/authorize", path):
        return method == "GET"
    if re.fullmatch(r"/v1/mcp/server/oauth/[^/]+/(register|token)", path):
        return method == "POST"
    if re.fullmatch(r"/v1/mcp/server/[^/]+/(oauth-user-credential|user-credential)", path):
        return method in ("POST", "DELETE")
    if re.fullmatch(r"/v1/mcp/server/[^/]+/oauth-user-credential/status", path):
        return method == "GET"
    return False


def authorize_browser_route(request: Request, role: str) -> None:
    from litellm.proxy.auth.auth_utils import get_request_route

    personal = is_personal_mcp_route(get_request_route(request), request.method)
    if role not in ("proxy_admin", "proxy_admin_viewer") and not personal:
        raise HTTPException(403, "This session has personal MCP access, not administration access")
    if role == "proxy_admin_viewer" and request.method not in ("GET", "HEAD", "OPTIONS") and not personal:
        raise HTTPException(403, "This role permits read-only administration")


async def authenticate_browser(request: Request, api_key: str | None) -> UserAPIKeyAuth | None:
    """Narrow dispatch; unrelated keys continue through the original authentication path."""
    if api_key != BROWSER_MARKER:
        return None
    config, user, state = await current_session(request)
    require_same_origin(request, config)
    authorize_browser_route(request, user.user_role)
    request.state.oidc_claims = state["claims"]
    request.state.oidc_user_id = user.user_id
    request.state.oidc_browser = True
    auth = UserAPIKeyAuth(
        user_id=user.user_id,
        user_role=user.user_role,
        user_email=user.user_email,
        org_id=user.organization_id,
        api_key=state["session_hash"],
        is_session_token=True,
        models=list(user.models or []),
        metadata={"oidc_browser": True},
    )
    auth.requires_fresh_policy = True
    auth.oidc_identity_source = "browser"
    auth.oidc_verified_claims = state["claims"]
    from litellm.proxy._experimental.mcp_server.oidc_policy import attach_user_permissions

    await attach_user_permissions(auth, user)
    return auth
