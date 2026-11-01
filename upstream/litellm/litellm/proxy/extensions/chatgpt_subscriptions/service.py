"""Read credentials and retrieve quota snapshots without refreshing tokens."""

import asyncio
import base64
import json
import math
import os
import time
from pathlib import Path

import httpx

from . import tokens

# Only overridden by the end-to-end tests.
USAGE_URL = os.getenv("CHATGPT_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage")
MODELS_URL = os.getenv("CHATGPT_MODELS_URL", "https://chatgpt.com/backend-api/codex/models")
# The models endpoint leaves out models that need a newer Codex CLI than this.
CODEX_CLIENT_VERSION = os.getenv("CHATGPT_CODEX_CLIENT_VERSION", "0.159.2")


class MonitoringError(Exception):
    """Only fixed, non-sensitive messages may cross the HTTP boundary."""


def number(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise MonitoringError("Invalid quota data from OpenAI.")
    return value


def claims(token):
    try:
        part = token.split(".")[1]
        result = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def read_credentials(path=None):
    path = Path(path or tokens.auth_path())
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise TypeError()
        token = data.get("access_token")
        if (
            not isinstance(token, str)
            or not token
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError()
        expiry = data.get("expires_at")
        if expiry is None:
            expiry = claims(token).get("exp")
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry)
        ):
            raise ValueError()
        if expiry <= time.time() + 60:
            raise MonitoringError(
                "ChatGPT token expired or expires soon. Renew it through LiteLLM."
            )
        account = data.get("account_id")
        if not account:
            auth = claims(data.get("id_token") or token).get(
                "https://api.openai.com/auth", {}
            )
            account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        if (
            not isinstance(account, str)
            or not account
            or any(ord(c) < 33 or ord(c) > 126 for c in account)
        ):
            raise ValueError()
        return token, account
    except MonitoringError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise MonitoringError(
            "ChatGPT credentials unavailable or invalid. Sign in through LiteLLM."
        ) from None


def window(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MonitoringError("Invalid quota window from OpenAI.")
    used = (
        number(value["used_percent"]) if value.get("used_percent") is not None else None
    )
    duration = (
        number(value["limit_window_seconds"])
        if value.get("limit_window_seconds") is not None
        else None
    )
    reset = number(value["reset_at"]) if value.get("reset_at") is not None else None
    if (duration is not None and duration <= 0) or (reset is not None and reset < 0):
        raise MonitoringError("Invalid quota window from OpenAI.")
    return {
        "used_percent": used,
        "remaining_percent": max(0, min(100, 100 - used)) if used is not None else None,
        "window_seconds": duration,
        "reset_at": reset,
    }


def group(identifier, details):
    if details is None:
        return {"id": identifier, "allowed": None, "primary": None, "secondary": None}
    if not isinstance(details, dict):
        raise MonitoringError("Invalid quota group from OpenAI.")
    allowed = details.get("allowed")
    if allowed is not None and not isinstance(allowed, bool):
        raise MonitoringError("Invalid quota availability from OpenAI.")
    return {
        "id": identifier,
        "allowed": allowed,
        "primary": window(details.get("primary_window")),
        "secondary": window(details.get("secondary_window")),
    }


def normalize(payload, account=tokens.DEFAULT_ACCOUNT):
    if not isinstance(payload, dict):
        raise MonitoringError("Invalid quota response from OpenAI.")
    groups = [group("codex", payload.get("rate_limit"))]
    additional = payload.get("additional_rate_limits") or []
    if not isinstance(additional, list):
        raise MonitoringError("Invalid additional quota groups from OpenAI.")
    for entry in additional:
        if not isinstance(entry, dict):
            raise MonitoringError("Invalid additional quota group from OpenAI.")
        identifier = entry.get("metered_feature")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise MonitoringError("Invalid quota identifier from OpenAI.")
        if any(g["id"] == identifier for g in groups):
            raise MonitoringError("Duplicate quota identifier from OpenAI.")
        groups.append(group(identifier, entry.get("rate_limit")))
    return {"account": account, "fetched_at": time.time(), "limits": groups}


def model_entry(entry):
    """The fields the admin page shows, from one entry of the Codex models list."""
    if not isinstance(entry, dict):
        raise MonitoringError("Invalid model entry from OpenAI.")
    slug = entry.get("slug")
    if not isinstance(slug, str) or not slug or len(slug) > 128:
        raise MonitoringError("Invalid model identifier from OpenAI.")
    name = entry.get("display_name")
    priority = entry.get("priority")
    context = entry.get("context_window")
    levels = entry.get("supported_reasoning_levels")
    return {
        "slug": slug,
        "name": name if isinstance(name, str) and name else slug,
        "visible": entry.get("visibility") == "list",
        "priority": number(priority) if priority is not None else None,
        "context_window": number(context) if context is not None else None,
        "reasoning": [
            level["effort"] for level in levels
            if isinstance(level, dict) and isinstance(level.get("effort"), str)
        ] if isinstance(levels, list) else [],
    }


def normalize_models(payload, account=tokens.DEFAULT_ACCOUNT):
    """Models of one subscription, in the order of the Codex CLI's model picker."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise MonitoringError("Invalid models response from OpenAI.")
    models = [model_entry(entry) for entry in payload["models"]]
    models.sort(key=lambda m: (m["priority"] is None, m["priority"] or 0, m["slug"]))
    return {"account": account, "fetched_at": time.time(), "models": models}


class CachedLookup:
    """One OpenAI lookup per subscription, cached so page reloads do not reach OpenAI."""

    ttl = 60
    error_ttl = 10
    failure = "ChatGPT request failed or timed out."

    def __init__(self, transport=None, account=tokens.DEFAULT_ACCOUNT, path=None):
        self.transport = transport
        self.account = account
        self.path = path
        self.lock = asyncio.Lock()
        self.cached = None
        self.error = None
        self.until = 0

    async def get(self):
        async with self.lock:
            if time.monotonic() < self.until:
                if self.error:
                    raise MonitoringError(self.error)
                return self.cached
            self.cached = None
            try:
                self.cached = await asyncio.wait_for(self.fetch(), timeout=10)
            except MonitoringError as exc:
                self.error = str(exc)
            except Exception:  # noqa: BLE001 - final boundary must not leak credential-bearing errors
                # Never expose HTTPX errors: they may contain request metadata.
                self.error = self.failure
            else:
                self.error = None
            self.until = time.monotonic() + (self.error_ttl if self.error else self.ttl)
            if self.error:
                raise MonitoringError(self.error)
            return self.cached

    async def credentials(self):
        return await asyncio.to_thread(
            read_credentials, self.path or tokens.auth_path(self.account)
        )


class ModelsService(CachedLookup):
    # The response is about 600 KB, mostly each model's instructions for the CLI.
    ttl = 300
    error_ttl = 60
    failure = "ChatGPT models request failed or timed out."

    async def fetch(self):
        token, account = await self.credentials()
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=self.transport
        ) as client:
            # Same identification as the Codex CLI, which this endpoint serves.
            response = await client.get(
                MODELS_URL,
                params={"client_version": CODEX_CLIENT_VERSION},
                headers={
                    "Authorization": "Bearer " + token,
                    "ChatGPT-Account-ID": account,
                    "originator": "codex_cli_rs",
                    "User-Agent": "codex_cli_rs/" + CODEX_CLIENT_VERSION,
                    "version": CODEX_CLIENT_VERSION,
                    "Accept": "application/json",
                },
            )
        if response.status_code == 401:
            raise MonitoringError(
                "ChatGPT authorization expired or rejected. Renew it through LiteLLM."
            )
        if response.status_code == 429:
            raise MonitoringError("OpenAI models endpoint rate limited. Try again later.")
        if response.status_code != 200:
            raise MonitoringError("OpenAI models endpoint unavailable.")
        try:
            payload = response.json()
        except ValueError:
            raise MonitoringError("Invalid JSON from OpenAI models endpoint.") from None
        return normalize_models(payload, self.account)


class UsageService(CachedLookup):
    failure = "ChatGPT quota request failed or timed out."

    async def fetch(self):
        token, account = await self.credentials()
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=self.transport
        ) as client:
            # Deliberately avoid LiteLLM's clients, hooks and request logging.
            response = await client.get(
                USAGE_URL,
                headers={
                    "Authorization": "Bearer " + token,
                    "ChatGPT-Account-Id": account,
                    "User-Agent": "codex-cli",
                    "Accept": "application/json",
                },
            )
        if response.status_code == 401:
            raise MonitoringError(
                "ChatGPT authorization expired or rejected. Renew it through LiteLLM."
            )
        if response.status_code == 429:
            raise MonitoringError(
                "OpenAI quota endpoint rate limited. Try again later."
            )
        if response.status_code != 200:
            raise MonitoringError("OpenAI quota endpoint unavailable.")
        try:
            payload = response.json()
        except ValueError:
            raise MonitoringError("Invalid JSON from OpenAI quota endpoint.") from None
        return normalize(payload, self.account)
