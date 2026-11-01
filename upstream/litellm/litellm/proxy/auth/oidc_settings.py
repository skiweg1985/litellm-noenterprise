"""Administration and authoritative request snapshots for independent OIDC."""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.oidc_identity import ClaimRule, OIDCSettings, RoleMapping
from litellm.proxy.auth.oidc_api import APISettings
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.repositories.config_repository import ConfigRepository

SETTINGS_KEY = "independent_oidc_settings"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class BrowserSettings(StrictModel):
    issuer: str
    client_id: str = Field(min_length=1)
    redirect_uri: str
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    subject_claim: str = "sub"
    access_rule: ClaimRule
    role_mappings: list[RoleMapping] = Field(default_factory=list)
    default_role: Literal["internal_user_viewer", "customer"] = "internal_user_viewer"
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    retained_claims: list[str] = Field(
        default_factory=lambda: ["roles", "groups", "scp", "scope"]
    )
    mcp_access_rules: dict[str, ClaimRule] = Field(default_factory=dict)
    allow_insecure_http: bool = False

    @model_validator(mode="after")
    def validate_identity(self):
        OIDCSettings.model_validate(
            {**self.model_dump(), "client_secret_env": "DATABASE_MANAGED"}
        )
        return self


class SecretStatus(StrictModel):
    configured: bool = False
    source: Literal["database", "environment", "unset"] = "unset"
    environment_variable: str | None = None


class Prerequisites(StrictModel):
    database_connected: bool
    master_key_configured: bool
    custom_auth_compatible: bool


class MCPServerChoice(StrictModel):
    server_id: str
    name: str
    alias: str | None = None


class SettingsResponse(StrictModel):
    revision: int = 0
    enabled: bool = False
    source: Literal["database", "file", "unset"] = "unset"
    read_only: bool = False
    read_only_reason: str | None = None
    browser: BrowserSettings | None = None
    api: APISettings | None = None
    client_secret: SecretStatus = Field(default_factory=SecretStatus)
    session_secret: SecretStatus = Field(default_factory=SecretStatus)
    prerequisites: Prerequisites
    legacy_sso_configured: bool = False
    mcp_servers: list[MCPServerChoice] = Field(default_factory=list)
    recovery_login_url: Literal["/fallback/login"] = "/fallback/login"


class SaveSettingsRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    browser: BrowserSettings
    api: APISettings | None
    client_secret: SecretStr | None = Field(default=None, min_length=1)


class SaveSettingsResponse(SettingsResponse):
    sessions_invalidated: bool


class CheckProviderRequest(StrictModel):
    issuer: str
    allow_insecure_http: bool = False


class CheckProviderResponse(StrictModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    token_endpoint_auth_methods_supported: list[str]
    id_token_signing_alg_values_supported: list[str]
    scopes_supported: list[str]
    code_challenge_methods_supported: list[str] | None
    credential_verified: Literal[False] = False


class DisableSettingsRequest(StrictModel):
    expected_revision: int = Field(ge=0)


class FieldError(StrictModel):
    path: list[str | int]
    message: str


class ErrorDetail(StrictModel):
    code: Literal[
        "invalid_configuration",
        "provider_unavailable",
        "revision_conflict",
        "file_managed",
        "prerequisite_missing",
        "configuration_unavailable",
    ]
    message: str
    errors: list[FieldError] = Field(default_factory=list)
    current_revision: int | None = None


class SettingsError(StrictModel):
    detail: ErrorDetail


def problem(status: int, code: str, message: str, *, errors=(), current_revision=None):
    raise HTTPException(
        status,
        {
            "code": code,
            "message": message,
            "errors": list(errors),
            **(
                {"current_revision": current_revision}
                if current_revision is not None
                else {}
            ),
        },
    )


class SanitizedRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    headers={"Cache-Control": "no-store"},
                    content={
                        "detail": {
                            "code": "invalid_configuration",
                            "message": "Correct the highlighted settings.",
                            "errors": [
                                {
                                    "path": list(error["loc"])[1:],
                                    "message": error["msg"],
                                }
                                for error in exc.errors()
                            ],
                        }
                    },
                )

        return handle


async def require_admin(
    request: Request, auth: UserAPIKeyAuth = Depends(user_api_key_auth)
):
    if (
        auth.user_role != LitellmUserRoles.PROXY_ADMIN
        or auth.oidc_identity_source == "provider"
    ):
        raise HTTPException(
            403, "Use a full administrator browser session or native administrator key."
        )
    return auth


router = APIRouter(
    prefix="/oidc/settings",
    tags=["Independent OIDC settings"],
    route_class=SanitizedRoute,
)


def prerequisites() -> Prerequisites:
    from litellm.proxy.proxy_server import prisma_client, master_key, general_settings

    return Prerequisites(
        database_connected=prisma_client is not None,
        master_key_configured=bool(master_key),
        custom_auth_compatible=not bool(general_settings.get("custom_auth")),
    )


def file_owned() -> bool:
    from litellm.proxy.proxy_server import proxy_config, general_settings

    store = proxy_config.settings
    return any(store.owned_by_config(key) for key in ("oidc_identity", "oidc_api")) or (
        isinstance(general_settings, dict)
        and any(key in general_settings for key in ("oidc_identity", "oidc_api"))
    )


def repository() -> ConfigRepository:
    from litellm.proxy.proxy_server import prisma_client

    return ConfigRepository(prisma_client, use_writer=True)


async def legacy_configured() -> bool:
    """Keep existing legacy setup discoverable without loading its credentials."""
    import json
    from litellm.proxy.auth.auth_utils import has_user_setup_sso
    from litellm.proxy.common_utils.encrypt_decrypt_utils import (
        decrypt_if_encrypted_with,
        get_salt_key,
    )
    from litellm.proxy.proxy_server import prisma_client
    from litellm.repositories.table_repositories import SSOConfigRepository

    if has_user_setup_sso():
        return True
    if prisma_client is None:
        return False
    try:
        record = await SSOConfigRepository(
            prisma_client, use_writer=True
        ).table.find_unique(where={"id": "sso_config"})
        if record is None:
            return False
        raw = record.sso_settings
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            return bool(raw)
        key = get_salt_key()
        for field in (
            "microsoft_client_id",
            "google_client_id",
            "generic_client_id",
            "saml_idp_metadata_url",
            "saml_idp_metadata_xml",
        ):
            value = raw.get(field)
            if not isinstance(value, str):
                continue
            decrypted = decrypt_if_encrypted_with(value, key) if key else None
            # Older legacy rows may contain plaintext identifying fields.
            if (decrypted if decrypted is not None else value).strip():
                return True
        return False
    except Exception:
        problem(
            503,
            "configuration_unavailable",
            "Legacy SSO status could not be read. Reload settings to check the active configuration.",
        )


async def describe(bundle: StoredBundle | None = None) -> SettingsResponse:
    from litellm.proxy.proxy_server import general_settings
    from litellm.proxy._experimental.mcp_server.mcp_server_manager import (
        global_mcp_server_manager,
    )

    result = SettingsResponse(
        prerequisites=prerequisites(), legacy_sso_configured=await legacy_configured()
    )
    result.mcp_servers = [
        MCPServerChoice(
            server_id=server.server_id, name=server.name, alias=server.alias
        )
        for server in global_mcp_server_manager.get_registry().values()
    ]
    if file_owned():
        result.source = "file"
        result.read_only = True
        result.read_only_reason = "OIDC is owned by the deployment configuration. Edit that file to change it."
        raw = general_settings.get("oidc_identity")
        if raw is not None:
            settings = OIDCSettings.model_validate(raw)
            result.enabled = True
            result.browser = BrowserSettings.model_validate(
                settings.model_dump(exclude={"client_secret_env", "session_secret_env"})
            )
            result.api = (
                APISettings.model_validate(general_settings["oidc_api"])
                if general_settings.get("oidc_api") is not None
                else None
            )
            result.client_secret = SecretStatus(
                configured=bool(os.environ.get(settings.client_secret_env)),
                source="environment",
                environment_variable=settings.client_secret_env,
            )
            result.session_secret = SecretStatus(
                configured=bool(os.environ.get(settings.session_secret_env)),
                source="environment",
                environment_variable=settings.session_secret_env,
            )
    else:
        bundle = bundle or await read_bundle()
        if bundle:
            result.source = "database"
            result.revision = bundle.revision
            result.enabled = bundle.enabled
            result.browser = bundle.browser
            result.api = bundle.api
            result.client_secret = SecretStatus(
                configured=bool(bundle.client_secret_ciphertext), source="database"
            )
            result.session_secret = SecretStatus(
                configured=bool(bundle.session_secret_ciphertext), source="database"
            )
    return result


@router.get("", response_model=SettingsResponse)
async def get_settings(response: Response, auth=Depends(require_admin)):
    response.headers["Cache-Control"] = "no-store"
    return await describe()


# One bundle is committed atomically. Plaintext never enters general_settings.
class StoredBundle(StrictModel):
    revision: int = Field(ge=1)
    enabled: bool
    browser: BrowserSettings
    api: APISettings | None
    client_secret_ciphertext: str
    session_secret_ciphertext: str


async def read_bundle() -> StoredBundle | None:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        return None
    try:
        row = await repository().get_param(SETTINGS_KEY)
        return StoredBundle.model_validate(row.param_value) if row else None
    except Exception:
        problem(
            503,
            "configuration_unavailable",
            "Stored OIDC configuration is unavailable. Use native administrator recovery.",
        )


def runtime_browser(bundle: StoredBundle) -> OIDCSettings:
    from litellm.proxy.common_utils.encrypt_decrypt_utils import (
        decrypt_if_encrypted_with,
        get_salt_key,
    )

    key = get_salt_key()
    client = (
        decrypt_if_encrypted_with(bundle.client_secret_ciphertext, key) if key else None
    )
    session = (
        decrypt_if_encrypted_with(bundle.session_secret_ciphertext, key)
        if key
        else None
    )
    if not client or not session or len(session) < 32:
        problem(
            503,
            "configuration_unavailable",
            "Stored OIDC credentials could not be decrypted. Use native administrator recovery.",
        )
    config = OIDCSettings.model_validate(
        {
            **bundle.browser.model_dump(),
            "client_secret_env": "DATABASE_MANAGED",
            "session_secret_env": "DATABASE_MANAGED",
        }
    )
    config._active_revision = bundle.revision
    config._client_secret = client
    config._session_secret = session
    return config


def validate_pair(browser: OIDCSettings, api: APISettings | None):
    from urllib.parse import urlparse

    if api is None:
        return
    if api.audience == browser.client_id and "JWT" in api.token_types:
        problem(
            422,
            "invalid_configuration",
            "API token audience must differ from the browser client ID for JWT tokens.",
            errors=[
                {
                    "path": ["api", "audience"],
                    "message": "Use a distinct API audience for generic JWT access tokens.",
                }
            ],
        )
    if urlparse(api.resource).scheme != "https" and not (
        browser.allow_insecure_http and urlparse(api.resource).scheme == "http"
    ):
        problem(
            422,
            "invalid_configuration",
            "API resource requires HTTPS.",
            errors=[
                {
                    "path": ["api", "resource"],
                    "message": "Use HTTPS, or enable isolated-development HTTP.",
                }
            ],
        )


async def check_provider(data: CheckProviderRequest) -> CheckProviderResponse:
    import httpx
    from pydantic import ValidationError
    from litellm.proxy.auth.oidc_identity import discovery

    try:
        config = OIDCSettings(
            issuer=data.issuer,
            client_id="discovery-check",
            client_secret_env="UNUSED",
            redirect_uri=data.issuer.rstrip("/") + "/callback",
            access_rule=ClaimRule(claim="roles", any_of=["unused"]),
            allow_insecure_http=data.allow_insecure_http,
        )
    except ValidationError:
        problem(
            422,
            "invalid_configuration",
            "Issuer must be an absolute HTTPS URL without credentials, query or fragment.",
            errors=[
                {"path": ["issuer"], "message": "Enter a valid provider issuer URL."}
            ],
        )
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            metadata = await discovery(config, client)
        methods = metadata.get(
            "token_endpoint_auth_methods_supported", ["client_secret_basic"]
        )
        algorithms = metadata.get("id_token_signing_alg_values_supported", ["RS256"])
        pkce = metadata.get("code_challenge_methods_supported")
        if pkce is not None and (not isinstance(pkce, list) or "S256" not in pkce):
            raise ValueError("unsupported PKCE methods")
        if not isinstance(methods, list) or not {
            "client_secret_basic",
            "client_secret_post",
        }.intersection(methods):
            raise ValueError("unsupported client authentication")
        if not isinstance(algorithms, list) or not {"RS256", "ES256"}.intersection(
            algorithms
        ):
            raise ValueError("unsupported signing algorithms")
        return CheckProviderResponse(
            issuer=metadata["issuer"],
            authorization_endpoint=metadata["authorization_endpoint"],
            token_endpoint=metadata["token_endpoint"],
            jwks_uri=metadata["jwks_uri"],
            token_endpoint_auth_methods_supported=methods,
            id_token_signing_alg_values_supported=algorithms,
            scopes_supported=metadata.get("scopes_supported", []),
            code_challenge_methods_supported=metadata.get(
                "code_challenge_methods_supported"
            ),
        )
    except (httpx.HTTPError, HTTPException, ValueError, TypeError, KeyError):
        problem(
            503,
            "provider_unavailable",
            "Provider discovery is unavailable or incompatible. Check the issuer and provider settings.",
            errors=[
                {"path": ["issuer"], "message": "Discovery could not be validated."}
            ],
        )


def writable():
    if file_owned():
        problem(
            409,
            "file_managed",
            "OIDC is managed by the deployment configuration and is read-only here.",
        )
    missing = prerequisites()
    if not all(missing.model_dump().values()):
        problem(
            503,
            "prerequisite_missing",
            "OIDC requires a database, master key and no replacing custom-auth hook.",
        )


def check_revision(expected: int, current: StoredBundle | None):
    revision = current.revision if current else 0
    if expected != revision:
        problem(
            409,
            "revision_conflict",
            "Settings changed while you were editing. Reload and review them.",
            current_revision=revision,
        )


async def commit_bundle(bundle: StoredBundle, expected_revision: int):
    try:
        saved = await repository().set_param_if_revision(
            SETTINGS_KEY, bundle.model_dump(mode="json"), expected_revision
        )
    except Exception:
        problem(
            503,
            "configuration_unavailable",
            "The save could not be confirmed. Reload settings to check the active configuration.",
        )
    if not saved:
        problem(
            409,
            "revision_conflict",
            "Settings changed while you were editing. Reload and review them.",
        )


@router.post("/check", response_model=CheckProviderResponse)
async def check_settings(
    data: CheckProviderRequest, response: Response, auth=Depends(require_admin)
):
    response.headers["Cache-Control"] = "no-store"
    return await check_provider(data)


@router.put("", response_model=SaveSettingsResponse)
async def save_settings(
    data: SaveSettingsRequest, response: Response, auth=Depends(require_admin)
):
    import secrets
    from litellm.proxy.common_utils.encrypt_decrypt_utils import encrypt_value_helper

    response.headers["Cache-Control"] = "no-store"
    writable()
    current = await read_bundle()
    check_revision(data.expected_revision, current)
    browser = OIDCSettings.model_validate(
        {**data.browser.model_dump(), "client_secret_env": "DATABASE_MANAGED"}
    )
    validate_pair(browser, data.api)
    if data.client_secret is None and current is None:
        problem(
            422,
            "invalid_configuration",
            "Enter a client credential for the first setup.",
            errors=[
                {
                    "path": ["client_secret"],
                    "message": "A client credential is required.",
                }
            ],
        )
    await check_provider(
        CheckProviderRequest(
            issuer=browser.issuer, allow_insecure_http=browser.allow_insecure_http
        )
    )
    replacement = (
        data.client_secret.get_secret_value()
        if data.client_secret is not None
        else None
    )
    if current:
        existing_runtime = runtime_browser(current)
        same_secret = replacement is None or secrets.compare_digest(
            replacement.encode("utf-8"),
            (existing_runtime.client_secret() or "").encode("utf-8"),
        )
        if (
            current.enabled
            and data.browser == current.browser
            and data.api == current.api
            and same_secret
        ):
            await commit_bundle(current, data.expected_revision)
            return SaveSettingsResponse(
                **(await describe(current)).model_dump(), sessions_invalidated=False
            )
    else:
        same_secret = False
    bundle = StoredBundle(
        revision=data.expected_revision + 1,
        enabled=True,
        browser=data.browser,
        api=data.api,
        client_secret_ciphertext=current.client_secret_ciphertext
        if current and same_secret
        else encrypt_value_helper(replacement),
        session_secret_ciphertext=current.session_secret_ciphertext
        if current
        else encrypt_value_helper(secrets.token_urlsafe(48)),
    )
    await commit_bundle(bundle, data.expected_revision)
    return SaveSettingsResponse(
        **(await describe(bundle)).model_dump(),
        sessions_invalidated=bool(current and current.enabled),
    )


@router.post("/disable", response_model=SaveSettingsResponse)
async def disable_settings(
    data: DisableSettingsRequest, response: Response, auth=Depends(require_admin)
):
    response.headers["Cache-Control"] = "no-store"
    writable()
    current = await read_bundle()
    check_revision(data.expected_revision, current)
    if current is None or not current.enabled:
        return SaveSettingsResponse(
            **(await describe(current)).model_dump(), sessions_invalidated=False
        )
    disabled = current.model_copy(
        update={"enabled": False, "revision": current.revision + 1}
    )
    await commit_bundle(disabled, data.expected_revision)
    return SaveSettingsResponse(
        **(await describe(disabled)).model_dump(), sessions_invalidated=True
    )


@dataclass(frozen=True)
class RuntimeSnapshot:
    browser: OIDCSettings | None
    api: APISettings | None
    error: HTTPException | None = None


current_snapshot: ContextVar[RuntimeSnapshot | None] = ContextVar(
    "independent_oidc_snapshot", default=None
)


async def load_snapshot() -> RuntimeSnapshot:
    from litellm.proxy.proxy_server import general_settings

    try:
        if file_owned():
            raw = general_settings.get("oidc_identity")
            browser = OIDCSettings.model_validate(raw) if raw is not None else None
            raw_api = general_settings.get("oidc_api")
            api = APISettings.model_validate(raw_api) if raw_api is not None else None
        else:
            bundle = await read_bundle()
            if bundle is None or not bundle.enabled:
                return RuntimeSnapshot(None, None)
            browser, api = runtime_browser(bundle), bundle.api
        if api is not None and browser is None:
            raise ValueError("API identity requires browser configuration")
        if browser is not None:
            validate_pair(browser, api)
            if not all(prerequisites().model_dump().values()):
                raise ValueError("OIDC prerequisites unavailable")
        return RuntimeSnapshot(browser, api)
    except Exception:
        problem(
            503,
            "configuration_unavailable",
            "OIDC configuration is unavailable. Native administrator recovery remains available.",
        )


class OIDCSettingsMiddleware:
    """Pin one authoritative bundle for auth and downstream MCP execution.

    An unavailable bundle is retained as an error, never interpreted as disabled.
    Its consumers fail closed while native administration can still recover.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "").rstrip("/")
        if (
            path == "/health/liveliness"
            or path == "/ui"
            or path.startswith(("/ui/", "/litellm-asset-prefix/", "/_next/"))
        ):
            return await self.app(scope, receive, send)
        try:
            snapshot = await load_snapshot()
        except HTTPException as exc:
            snapshot = RuntimeSnapshot(None, None, error=exc)

        async def settings_send(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() != b"cache-control"
                    ]
                    + [(b"cache-control", b"no-store")],
                }
            await send(message)

        response_send = (
            settings_send
            if path == "/oidc/settings" or path.startswith("/oidc/settings/")
            else send
        )
        token = current_snapshot.set(snapshot)
        try:
            return await self.app(scope, receive, response_send)
        finally:
            current_snapshot.reset(token)
