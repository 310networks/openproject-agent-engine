"""Async OpenProject API v3 client.

Verified behaviour (see ``docs/VERIFICATION.md``):

* Auth is HTTP Basic with username ``apikey`` and the token as the password.
* Collections expose ``_embedded.elements`` and offset pagination. OpenProject
  returns ``_links.nextByOffset`` (not ``_links.next``) for the next page.
* ``GET /api/v3/work_packages/{id}/relations`` is deprecated and redirects, so
  redirects are followed.
* Transient failures (network errors, HTTP 429, 5xx) are retried with
  exponential backoff and jitter, up to five attempts.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, NoReturn
from urllib.parse import urljoin

import httpx

from op_agent_engine.logging import get_logger

JSON = Any
SleepFunction = Callable[[float], Awaitable[None]]

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 30.0
BODY_EXCERPT_LIMIT = 500


class OpenProjectError(Exception):
    """Base class for OpenProject failures."""


class OpenProjectAuthError(OpenProjectError):
    """Authentication or authorization failed (401/403)."""

    def __init__(self, message: str, *, status_code: int, url: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class OpenProjectNotFoundError(OpenProjectError):
    """The requested resource does not exist (404)."""

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class OpenProjectHTTPError(OpenProjectError):
    """Any other non-success HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        url: str,
        method: str,
        body_excerpt: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.method = method
        self.body_excerpt = body_excerpt


class OpenProjectClient:
    """Thin async wrapper around the OpenProject API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFunction = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not token:
            raise ValueError("token must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self._base_url = base_url.rstrip("/") + "/"
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._log = get_logger("op_agent_engine.openproject.client")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=httpx.BasicAuth("apikey", token),
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
            headers={"Accept": "application/hal+json"},
        )

    async def __aenter__(self) -> OpenProjectClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- HTTP verbs ------------------------------------------------------

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> JSON:
        response = await self._request("GET", path, params=params)
        return self._decode(response)

    async def post_json(self, path: str, *, json_body: JSON | None = None) -> JSON:
        response = await self._request("POST", path, json_body=json_body)
        return self._decode(response)

    async def patch_json(self, path: str, *, json_body: JSON | None = None) -> JSON:
        response = await self._request("PATCH", path, json_body=json_body)
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> JSON:
        try:
            return response.json()
        except ValueError as error:
            raise OpenProjectError(
                f"invalid JSON from {response.request.method} {response.request.url}"
            ) from error

    # -- Collections -----------------------------------------------------

    async def iter_collection(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, JSON]]:
        """Yield every element of a paginated OpenProject collection."""
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        next_url: str | None = path
        next_params: Mapping[str, str | int] | None = dict(params or {})
        if next_params is not None:
            next_params = {**next_params, "pageSize": page_size}
        seen: set[str] = set()

        while next_url is not None:
            payload = await self.get_json(next_url, params=next_params)
            if not isinstance(payload, dict):
                raise OpenProjectError(
                    f"expected a JSON object from {self._absolute(next_url)}, "
                    f"got {type(payload).__name__}"
                )
            embedded = payload.get("_embedded")
            elements = embedded.get("elements") if isinstance(embedded, dict) else None
            if not isinstance(elements, list):
                raise OpenProjectError(
                    f"malformed collection response from {self._absolute(next_url)}: "
                    "missing _embedded.elements"
                )
            for element in elements:
                if not isinstance(element, dict):
                    raise OpenProjectError(
                        f"malformed collection element from {self._absolute(next_url)}"
                    )
                yield element

            links = payload.get("_links")
            next_href = self._next_href(links)
            if next_href is None:
                break
            if next_href in seen:
                raise OpenProjectError(
                    f"pagination loop detected while reading {self._absolute(path)}"
                )
            seen.add(next_href)
            next_url = next_href
            next_params = None

    async def get_collection(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        page_size: int = 100,
    ) -> list[dict[str, JSON]]:
        return [
            element
            async for element in self.iter_collection(
                path, params=params, page_size=page_size
            )
        ]

    # -- Internals -------------------------------------------------------

    def _absolute(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self._base_url, path.lstrip("/"))

    @staticmethod
    def _next_href(links: JSON) -> str | None:
        if not isinstance(links, dict):
            return None
        for key in ("nextByOffset", "next"):
            link = links.get(key)
            if isinstance(link, dict):
                href = link.get("href")
                if isinstance(href, str) and href:
                    return href
        return None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: JSON | None = None,
    ) -> httpx.Response:
        url = self._absolute(path)
        attempt = 0

        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    method, url, params=params, json=json_body
                )
            except httpx.TransportError as error:
                if attempt >= self._max_attempts:
                    raise OpenProjectError(
                        f"{method} {url} failed after {attempt} attempts: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                self._log.warning(
                    "OpenProject request failed; retrying",
                    extra={
                        "method": method,
                        "url": url,
                        "attempt": attempt,
                        "error": type(error).__name__,
                    },
                )
                await self._backoff(attempt, None)
                continue

            if response.is_success:
                return response

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_attempts:
                self._log.warning(
                    "OpenProject returned a retryable status; retrying",
                    extra={
                        "method": method,
                        "url": url,
                        "status_code": response.status_code,
                        "attempt": attempt,
                    },
                )
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            self._raise_for_response(method, url, response)

    def _raise_for_response(
        self, method: str, url: str, response: httpx.Response
    ) -> NoReturn:
        message = self._error_message(response)
        if response.status_code in (401, 403):
            raise OpenProjectAuthError(
                f"{method} {url} failed with HTTP {response.status_code}: {message}",
                status_code=response.status_code,
                url=url,
            )
        if response.status_code == 404:
            raise OpenProjectNotFoundError(
                f"{method} {url} failed with HTTP 404: {message}",
                url=url,
            )
        raise OpenProjectHTTPError(
            f"{method} {url} failed with HTTP {response.status_code}: {message}",
            status_code=response.status_code,
            url=url,
            method=method,
            body_excerpt=response.text[:BODY_EXCERPT_LIMIT],
        )

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.reason_phrase or "no response body"
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return response.reason_phrase or "no response body"

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self._retry_after_seconds(retry_after)
        if delay is None:
            base = min(MAX_BACKOFF_SECONDS, 0.5 * (2 ** (attempt - 1)))
            delay = base * (0.5 + self._rng.random())
        await self._sleep(delay)

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(tz=UTC)).total_seconds()
        return max(0.0, min(seconds, MAX_BACKOFF_SECONDS))


def json_dumps(value: JSON) -> str:
    """Compact JSON for OpenProject ``filters`` / ``sortBy`` query parameters."""
    return json.dumps(value, separators=(",", ":"))
