"""ChatGPT OAuth calls, mirroring litellm/llms/chatgpt/authenticator.py.

Unlike LiteLLM, failures are classified: a rejected refresh token needs a new
sign-in, a network error does not. Messages never contain server responses.
"""

import os

import httpx

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODE_VALIDITY_SECONDS = 15 * 60
TIMEOUT = 30


def auth_base():
    # Only overridden by the end-to-end tests.
    return os.getenv("CHATGPT_AUTH_BASE", "https://auth.openai.com").rstrip("/")


def verification_url():
    return auth_base() + "/codex/device"


class OAuthError(Exception):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def _tokens(response, required):
    try:
        data = response.json()
    except ValueError:
        raise OAuthError("Invalid response from the ChatGPT sign-in service.") from None
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data.get(key) for key in required
    ):
        raise OAuthError("Incomplete response from the ChatGPT sign-in service.")
    return data


def refresh(refresh_token, transport=None):
    try:
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=False, transport=transport
        ) as client:
            response = client.post(
                auth_base() + "/oauth/token",
                json={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "openid profile email",
                },
            )
    except httpx.HTTPError:
        raise OAuthError("ChatGPT sign-in service unreachable.") from None
    if response.status_code in (400, 401, 403):
        raise OAuthError(
            "Refresh token rejected. A new sign-in is required.", permanent=True
        )
    if response.status_code != 200:
        raise OAuthError(f"ChatGPT sign-in service returned {response.status_code}.")
    return _tokens(response, ("access_token", "id_token"))


class DeviceFlow:
    """One device-code sign-in: request a code, poll, exchange."""

    def __init__(self, transport=None):
        self.client = httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=False, transport=transport
        )

    async def close(self):
        await self.client.aclose()

    async def _post(self, path, **kwargs):
        try:
            return await self.client.post(auth_base() + path, **kwargs)
        except httpx.HTTPError:
            raise OAuthError("ChatGPT sign-in service unreachable.") from None

    async def request_code(self):
        response = await self._post(
            "/api/accounts/deviceauth/usercode", json={"client_id": CLIENT_ID}
        )
        if response.status_code != 200:
            raise OAuthError(f"Device code request returned {response.status_code}.")
        data = _tokens(response, ("device_auth_id",))
        user_code = data.get("user_code") or data.get("usercode")
        if not isinstance(user_code, str) or not user_code:
            raise OAuthError("Incomplete response from the ChatGPT sign-in service.")
        # The caller enforces a minimum poll interval.
        try:
            interval = max(0, int(data.get("interval", 5)))
        except (TypeError, ValueError):
            interval = 5
        return {
            "device_auth_id": data["device_auth_id"],
            "user_code": user_code,
            "interval": interval,
        }

    async def poll(self, code):
        """The authorization code once the user confirmed, else None."""
        response = await self._post(
            "/api/accounts/deviceauth/token",
            json={
                "device_auth_id": code["device_auth_id"],
                "user_code": code["user_code"],
            },
        )
        if response.status_code in (403, 404):
            return None
        if response.status_code != 200:
            raise OAuthError(f"Device authorization returned {response.status_code}.")
        return _tokens(response, ("authorization_code", "code_verifier"))

    async def exchange(self, authorization):
        response = await self._post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": authorization["authorization_code"],
                "redirect_uri": auth_base() + "/deviceauth/callback",
                "client_id": CLIENT_ID,
                "code_verifier": authorization["code_verifier"],
            },
        )
        if response.status_code != 200:
            raise OAuthError(f"Token exchange returned {response.status_code}.")
        return _tokens(response, ("access_token", "refresh_token", "id_token"))
