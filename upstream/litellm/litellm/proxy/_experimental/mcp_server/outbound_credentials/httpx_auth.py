"""Concrete `httpx2.Auth` objects the resolver returns for the self-contained modes.

These are the egress credential as the SDK consumes it: an `httpx2.Auth` attached to the
upstream `AsyncClient`. The OAuth-flow modes (`authorization_code`, `client_credentials`,
`token_exchange`) return SDK-provided auth objects instead and land later.

`auth_flow` mutating the outbound request is the `httpx2.Auth` contract, not a house-style
violation: the request is httpx2's object, and these carry no state of their own.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Generator

import httpx2
from pydantic import SecretStr


class NoOpAuth(httpx2.Auth):
    """Attaches nothing — the `none` mode (and the seam-level default)."""

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        yield request


class StaticHeaderAuth(httpx2.Auth):
    """Sets one fixed header on every request — the `api_key` family and `passthrough`.

    The header value is a live credential (a bearer token, an API key, a forwarded user
    token), so it is held as a `SecretStr` and unwrapped only when written onto the request.
    That keeps it masked in reprs, `vars()`, tracebacks, and structured logs, matching the
    `SecretStr` discipline the config models use.
    """

    def __init__(self, header_value: str, header_name: str = "Authorization") -> None:
        self.header_name = header_name
        self._header_value = SecretStr(header_value)

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers[self.header_name] = self._header_value.get_secret_value()
        yield request


class PersonalOAuthAuth(httpx2.Auth):
    """Renew a rejected personal grant once; never loop on an upstream rejection."""

    def __init__(
        self,
        inner: StaticHeaderAuth,
        recover: Callable[[str], Awaitable[StaticHeaderAuth | None]],
        rejected: Callable[[str], Awaitable[None]],
    ) -> None:
        self._inner = inner
        self._recover = recover
        self._rejected = rejected
        self.header_name = inner.header_name

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        flow = self._inner.async_auth_flow(request)
        try:
            outbound = await anext(flow)
            response = yield outbound
            if response.status_code != 401:
                return
            refreshed = await self._recover(outbound.headers[self.header_name])
            if refreshed is None:
                return
            await response.aread()
            self._inner = refreshed
            retry_flow = refreshed.async_auth_flow(request)
            try:
                retry = await anext(retry_flow)
                response = yield retry
                if response.status_code == 401:
                    await self._rejected(retry.headers[self.header_name])
            finally:
                await retry_flow.aclose()
        finally:
            await flow.aclose()
