import base64
import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from typing import Final, TypeAlias

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from litellm._logging import verbose_logger
from litellm.constants import HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS
from litellm.litellm_core_utils.asyncify import can_block_current_thread
from litellm.litellm_core_utils.request_timeout_resolver import get_configured_request_timeout
from litellm.llms.custom_httpx.http_handler import get_httpx_client

from .common_utils import (
    CHATGPT_API_BASE,
    CHATGPT_AUTH_BASE,
    CHATGPT_CLIENT_ID,
    CHATGPT_DEVICE_CODE_URL,
    CHATGPT_DEVICE_TOKEN_URL,
    CHATGPT_DEVICE_VERIFY_URL,
    CHATGPT_OAUTH_TOKEN_URL,
    GetAccessTokenError,
    GetDeviceCodeError,
    RefreshAccessTokenError,
)

TOKEN_EXPIRY_SKEW_SECONDS: Final = 60
TOKEN_REFRESH_TIMEOUT_SECONDS: Final = 30
DEVICE_CODE_TIMEOUT_SECONDS: Final = 15 * 60
DEVICE_CODE_COOLDOWN_SECONDS: Final = 5 * 60
DEVICE_CODE_POLL_SLEEP_SECONDS: Final = 5

OPENAI_AUTH_CLAIM_KEY: Final = "https://api.openai.com/auth"

# Subscription authentication: lock bookkeeping per thread; rejected refresh tokens per file.
_AUTH_LOCKS: Final = threading.local()
_REJECTED_REFRESH: Final[dict[str, str]] = {}
AUTH_LOCK_TIMEOUT_SECONDS: Final = 30

JsonObject: TypeAlias = Mapping[str, JsonValue]

_JSON_OBJECT_ADAPTER: Final = TypeAdapter(JsonObject)


def _optional_str(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _token_refresh_timeout() -> httpx.Timeout:
    configured: Final = get_configured_request_timeout()
    seconds: Final = (
        TOKEN_REFRESH_TIMEOUT_SECONDS if configured is None else min(configured, TOKEN_REFRESH_TIMEOUT_SECONDS)
    )
    return httpx.Timeout(seconds, connect=HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS)


# Subscription authentication: several subscriptions, one token file each. A deployment
# picks one with api_key "chatgpt-account:<name>", usually from a LiteLLM
# credential. Same layout as litellm/proxy/extensions/chatgpt_subscriptions/tokens.py.
ACCOUNT_KEY_PREFIX: Final = "chatgpt-account:"
DEFAULT_ACCOUNT: Final = "default"
_ACCOUNT_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_AUTHENTICATORS: Final[dict[str, "Authenticator"]] = {}


def configured_accounts() -> list[str]:
    names = os.getenv("CHATGPT_ACCOUNTS") or DEFAULT_ACCOUNT
    return [name.strip() for name in names.split(",") if name.strip()]


def account_for_api_key(api_key: object) -> str | None:
    if isinstance(api_key, str) and api_key.startswith(ACCOUNT_KEY_PREFIX):
        return api_key[len(ACCOUNT_KEY_PREFIX):]
    return None


class Authenticator:
    def __init__(self, account: str = DEFAULT_ACCOUNT) -> None:
        # Subscription authentication: the default account keeps the original path.
        if not _ACCOUNT_NAME.fullmatch(account):
            raise ValueError(f"Invalid ChatGPT account name: {account!r}")
        self.account = account
        base_dir = os.getenv(
            "CHATGPT_TOKEN_DIR",
            os.path.expanduser("~/.config/litellm/chatgpt"),
        )
        if account == DEFAULT_ACCOUNT:
            self.token_dir = base_dir
        else:
            self.token_dir = os.path.join(base_dir, "accounts", account)
        self.auth_file = os.path.join(self.token_dir, os.getenv("CHATGPT_AUTH_FILE", "auth.json"))
        self._ensure_token_dir()

    @classmethod
    def for_request(cls, litellm_params: object = None, api_key: object = None) -> "Authenticator":
        # Subscription authentication: the authenticator of the deployment's subscription.
        if api_key is None:
            if isinstance(litellm_params, Mapping):
                api_key = litellm_params.get("api_key")
            else:
                api_key = getattr(litellm_params, "api_key", None)
        account: Final = account_for_api_key(api_key)
        if account is None:
            # A missing or mistyped credential must not fall back to a
            # subscription someone else signed in to.
            raise GetAccessTokenError(
                message=(
                    "No ChatGPT subscription for this deployment. Reference a credential "
                    "with api_key chatgpt-account:<name> via litellm_credential_name."
                ),
                status_code=401,
            )
        if not _ACCOUNT_NAME.fullmatch(account) or account not in configured_accounts():
            raise GetAccessTokenError(
                message=f"Unknown ChatGPT account {account!r}. Add it to CHATGPT_ACCOUNTS.",
                status_code=401,
            )
        authenticator = _AUTHENTICATORS.get(account)
        if authenticator is None:
            authenticator = _AUTHENTICATORS.setdefault(account, cls(account))
        return authenticator

    def get_api_base(self) -> str:
        return os.getenv("CHATGPT_API_BASE") or os.getenv("OPENAI_CHATGPT_API_BASE") or CHATGPT_API_BASE

    def get_access_token(self) -> str:
        # Subscription authentication: never start a device-code sign-in inside a request.
        auth_data: Final = self._read_auth_file()
        if auth_data:
            access_token: Final = _optional_str(auth_data.get("access_token"))
            if access_token and not self._is_token_expired(auth_data, access_token):
                return access_token
        refreshed: Final = self._refresh_under_lock()
        if refreshed:
            return refreshed
        raise GetAccessTokenError(
            message="ChatGPT sign-in required. Sign in on /chatgpt/ui of this proxy.",
            status_code=401,
        )

    def get_account_id(self) -> str | None:
        auth_data: Final = self._read_auth_file()
        if not auth_data:
            return None
        account_id: Final = _optional_str(auth_data.get("account_id"))
        if account_id:
            return account_id
        id_token: Final = auth_data.get("id_token")
        access_token: Final = auth_data.get("access_token")
        derived: Final = self._extract_account_id(_optional_str(id_token or access_token))
        if derived:
            self._merge_auth_file(auth_data, {"account_id": derived})  # Subscription authentication
        return derived

    def _ensure_token_dir(self) -> None:
        if not os.path.exists(self.token_dir):
            os.makedirs(self.token_dir, exist_ok=True)

    def _read_auth_file(self) -> JsonObject | None:
        try:
            with open(self.auth_file, "r") as f:
                return _JSON_OBJECT_ADAPTER.validate_python(json.load(f))
        except OSError:
            return None
        except (json.JSONDecodeError, ValidationError) as exc:
            verbose_logger.warning("Invalid ChatGPT auth file: %s", exc)
            return None

    def _write_auth_file(self, data: JsonObject) -> None:
        # Subscription authentication: replace atomically under the lock.
        try:
            with self._auth_file_lock():
                fd, tmp = tempfile.mkstemp(
                    prefix="." + os.path.basename(self.auth_file) + ".", dir=self.token_dir
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(data, f)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.auth_file)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                    raise
        except (OSError, GetAccessTokenError) as exc:
            verbose_logger.error("Failed to write ChatGPT auth file: %s", exc)

    @contextlib.contextmanager
    def _auth_file_lock(self):
        # Subscription authentication: exclusive flock on "<auth file>.lock", re-entrant per thread.
        held = getattr(_AUTH_LOCKS, "paths", None)
        if held is None:
            held = _AUTH_LOCKS.paths = {}
        if held.get(self.auth_file):
            held[self.auth_file] += 1
            try:
                yield
            finally:
                held[self.auth_file] -= 1
            return
        fd = os.open(self.auth_file + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + AUTH_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GetAccessTokenError(
                            message="ChatGPT token file is locked by another process.",
                            status_code=503,
                        ) from None
                    time.sleep(0.05)
            held[self.auth_file] = 1
            try:
                yield
            finally:
                held[self.auth_file] = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _refresh_under_lock(self) -> str | None:
        # Subscription authentication: one refresh at a time; the others reuse its result.
        with self._auth_file_lock():
            auth_data = self._read_auth_file()
            if not auth_data:
                return None
            access_token = _optional_str(auth_data.get("access_token"))
            if access_token and not self._is_token_expired(auth_data, access_token):
                return access_token
            refresh_token = _optional_str(auth_data.get("refresh_token"))
            if not refresh_token or _REJECTED_REFRESH.get(self.auth_file) == refresh_token:
                return None
            try:
                return self._refresh_tokens(refresh_token)["access_token"]
            except RefreshAccessTokenError as exc:
                if exc.status_code in (400, 401, 403):
                    _REJECTED_REFRESH[self.auth_file] = refresh_token
                verbose_logger.warning("ChatGPT refresh token failed, sign-in required: %s", exc)
                return None

    def _merge_auth_file(self, seen: JsonObject, changes: dict[str, JsonValue]) -> None:
        # Subscription authentication: never overwrite tokens that changed in the meantime.
        try:
            with self._auth_file_lock():
                current = self._read_auth_file()
                if current is None or current.get("access_token") != seen.get("access_token"):
                    return
                self._write_auth_file({**current, **changes})
        except GetAccessTokenError:
            return

    def _is_token_expired(self, auth_data: JsonObject, access_token: str) -> bool:
        stored_expires_at: Final = auth_data.get("expires_at")
        if isinstance(stored_expires_at, (int, float)):
            return time.time() >= float(stored_expires_at) - TOKEN_EXPIRY_SKEW_SECONDS
        derived_expires_at: Final = self._get_expires_at(access_token)
        if derived_expires_at:
            self._merge_auth_file(auth_data, {"expires_at": derived_expires_at})  # Subscription authentication
        if derived_expires_at is None:
            return True
        return time.time() >= float(derived_expires_at) - TOKEN_EXPIRY_SKEW_SECONDS

    def _get_expires_at(self, token: str) -> int | None:
        claims: Final = self._decode_jwt_claims(token)
        exp: Final = claims.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp)
        return None

    def _decode_jwt_claims(self, token: str) -> JsonObject:
        try:
            parts: Final = token.split(".")
            if len(parts) < 2:
                return {}
            payload_b64: Final = parts[1] + "=" * (-len(parts[1]) % 4)
            payload_bytes: Final = base64.urlsafe_b64decode(payload_b64)
            return _JSON_OBJECT_ADAPTER.validate_python(json.loads(payload_bytes.decode("utf-8")))
        except Exception:
            return {}

    def _extract_account_id(self, token: str | None) -> str | None:
        if not token:
            return None
        claims: Final = self._decode_jwt_claims(token)
        auth_claims: Final = claims.get(OPENAI_AUTH_CLAIM_KEY)
        if isinstance(auth_claims, dict):
            account_id: Final = auth_claims.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
        return None

    def _login_device_code(self) -> dict[str, str]:
        cooldown_remaining: Final = self._get_device_code_cooldown_remaining(self._read_auth_file())
        if cooldown_remaining > 0:
            token: Final = self._wait_for_access_token(cooldown_remaining)
            if token:
                return {"access_token": token}

        device_code: Final = self._request_device_code()
        self._record_device_code_request()
        print(  # noqa: T201
            "Sign in with ChatGPT using device code:\n"
            f"1) Visit {CHATGPT_DEVICE_VERIFY_URL}\n"
            f"2) Enter code: {device_code['user_code']}\n"
            "Device codes are a common phishing target. Never share this code.",
            flush=True,
        )
        auth_code: Final = self._poll_for_authorization_code(device_code)
        tokens: Final = self._exchange_code_for_tokens(auth_code)
        auth_data: Final = self._build_auth_record(tokens)
        self._write_auth_file(auth_data)
        return tokens

    def _request_device_code(self) -> dict[str, str]:
        try:
            client: Final = get_httpx_client()
            resp: Final = client.post(
                CHATGPT_DEVICE_CODE_URL,
                json={"client_id": CHATGPT_CLIENT_ID},
            )
            resp.raise_for_status()
            data: Final = _JSON_OBJECT_ADAPTER.validate_python(resp.json())
        except httpx.HTTPStatusError as exc:
            raise GetDeviceCodeError(
                message=f"Failed to request device code: {exc}",
                status_code=exc.response.status_code,
            )
        except Exception as exc:
            raise GetDeviceCodeError(
                message=f"Failed to request device code: {exc}",
                status_code=400,
            )

        device_auth_id: Final = _optional_str(data.get("device_auth_id"))
        user_code: Final = _optional_str(data.get("user_code") or data.get("usercode"))
        interval: Final = data.get("interval")
        if not device_auth_id or not user_code:
            raise GetDeviceCodeError(
                message=f"Device code response missing fields: {data}",
                status_code=400,
            )
        return {
            "device_auth_id": device_auth_id,
            "user_code": user_code,
            "interval": str(interval or "5"),
        }

    def _poll_for_authorization_code(self, device_code: dict[str, str]) -> dict[str, str]:
        client: Final = get_httpx_client()
        interval: Final = int(device_code.get("interval", "5"))
        start_time: Final = time.time()
        while time.time() - start_time < DEVICE_CODE_TIMEOUT_SECONDS:
            try:
                resp = client.post(
                    CHATGPT_DEVICE_TOKEN_URL,
                    json={
                        "device_auth_id": device_code["device_auth_id"],
                        "user_code": device_code["user_code"],
                    },
                )
                if resp.status_code == 200:
                    data = _JSON_OBJECT_ADAPTER.validate_python(resp.json())
                    authorization_code = _optional_str(data.get("authorization_code"))
                    code_challenge = _optional_str(data.get("code_challenge"))
                    code_verifier = _optional_str(data.get("code_verifier"))
                    if authorization_code and code_challenge and code_verifier:
                        return {
                            "authorization_code": authorization_code,
                            "code_challenge": code_challenge,
                            "code_verifier": code_verifier,
                        }
                if resp.status_code in (403, 404):
                    time.sleep(max(interval, DEVICE_CODE_POLL_SLEEP_SECONDS))
                    continue
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response else None
                if status_code in (403, 404):
                    time.sleep(max(interval, DEVICE_CODE_POLL_SLEEP_SECONDS))
                    continue
                raise GetAccessTokenError(
                    message=f"Polling failed: {exc}",
                    status_code=exc.response.status_code,
                )
            except Exception as exc:
                raise GetAccessTokenError(
                    message=f"Polling failed: {exc}",
                    status_code=400,
                )
            time.sleep(max(interval, DEVICE_CODE_POLL_SLEEP_SECONDS))

        raise GetAccessTokenError(
            message="Timed out waiting for device authorization",
            status_code=408,
        )

    def _exchange_code_for_tokens(self, code_data: dict[str, str]) -> dict[str, str]:
        try:
            client: Final = get_httpx_client()
            redirect_uri: Final = f"{CHATGPT_AUTH_BASE}/deviceauth/callback"
            body: Final = (
                "grant_type=authorization_code"
                f"&code={code_data['authorization_code']}"
                f"&redirect_uri={redirect_uri}"
                f"&client_id={CHATGPT_CLIENT_ID}"
                f"&code_verifier={code_data['code_verifier']}"
            )
            resp: Final = client.post(
                CHATGPT_OAUTH_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                content=body,
            )
            resp.raise_for_status()
            data: Final = _JSON_OBJECT_ADAPTER.validate_python(resp.json())
        except httpx.HTTPStatusError as exc:
            raise GetAccessTokenError(
                message=f"Token exchange failed: {exc}",
                status_code=exc.response.status_code,
            )
        except Exception as exc:
            raise GetAccessTokenError(
                message=f"Token exchange failed: {exc}",
                status_code=400,
            )

        access_token: Final = _optional_str(data.get("access_token"))
        refresh_token: Final = _optional_str(data.get("refresh_token"))
        id_token: Final = _optional_str(data.get("id_token"))
        if not access_token or not refresh_token or not id_token:
            raise GetAccessTokenError(
                message=f"Token exchange response missing fields: {data}",
                status_code=400,
            )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
        }

    def _refresh_tokens(self, refresh_token: str) -> dict[str, str]:
        try:
            client: Final = get_httpx_client()
            resp: Final = client.post(
                CHATGPT_OAUTH_TOKEN_URL,
                json={
                    "client_id": CHATGPT_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "openid profile email",
                },
                timeout=_token_refresh_timeout(),
            )
            resp.raise_for_status()
            data: Final = _JSON_OBJECT_ADAPTER.validate_python(resp.json())
        except httpx.HTTPStatusError as exc:
            raise RefreshAccessTokenError(
                message="ChatGPT refresh request was rejected.",
                status_code=exc.response.status_code,
            )
        except httpx.HTTPError:
            raise RefreshAccessTokenError(
                message="ChatGPT refresh service is unavailable.",
                status_code=503,
            ) from None
        except Exception:
            raise RefreshAccessTokenError(
                message="Invalid response from the ChatGPT refresh service.",
                status_code=502,
            ) from None

        access_token: Final = _optional_str(data.get("access_token"))
        id_token: Final = _optional_str(data.get("id_token"))
        if not access_token or not id_token:
            raise RefreshAccessTokenError(
                message="Incomplete response from the ChatGPT refresh service.",
                status_code=502,
            )

        refreshed: Final = {
            "access_token": access_token,
            "refresh_token": _optional_str(data.get("refresh_token")) or refresh_token,
            "id_token": id_token,
        }
        auth_data: Final = self._build_auth_record(refreshed)
        self._write_auth_file(auth_data)
        return refreshed

    def _build_auth_record(self, tokens: dict[str, str]) -> JsonObject:
        access_token: Final = tokens.get("access_token")
        id_token: Final = tokens.get("id_token")
        expires_at: Final = self._get_expires_at(access_token) if access_token else None
        account_id: Final = self._extract_account_id(id_token or access_token)
        return {
            "access_token": access_token,
            "refresh_token": tokens.get("refresh_token"),
            "id_token": id_token,
            "expires_at": expires_at,
            "account_id": account_id,
        }

    def _get_device_code_cooldown_remaining(self, auth_data: JsonObject | None) -> float:
        if not auth_data:
            return 0.0
        requested_at: Final = auth_data.get("device_code_requested_at")
        if not isinstance(requested_at, (int, float, str)):
            return 0.0
        try:
            requested_seconds: Final = float(requested_at)
        except (TypeError, ValueError):
            return 0.0
        elapsed: Final = time.time() - requested_seconds
        remaining: Final = DEVICE_CODE_COOLDOWN_SECONDS - elapsed
        return max(0.0, remaining)

    def _record_device_code_request(self) -> None:
        # Subscription authentication: read and write under one lock.
        with self._auth_file_lock():
            auth_data: Final = self._read_auth_file() or {}
            self._write_auth_file({**auth_data, "device_code_requested_at": time.time()})

    def _wait_for_access_token(self, timeout_seconds: float) -> str | None:
        deadline: Final = time.time() + timeout_seconds
        while time.time() < deadline:
            auth_data = self._read_auth_file()
            if auth_data:
                access_token = _optional_str(auth_data.get("access_token"))
                if access_token and not self._is_token_expired(auth_data, access_token):
                    return access_token
            sleep_for = min(DEVICE_CODE_POLL_SLEEP_SECONDS, max(0.0, deadline - time.time()))
            if sleep_for <= 0:
                break
            time.sleep(sleep_for)
        return None
