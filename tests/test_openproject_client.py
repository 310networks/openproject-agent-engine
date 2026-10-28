from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable

import httpx
import pytest
import respx

from op_agent_engine.openproject.client import (
    OpenProjectAuthError,
    OpenProjectClient,
    OpenProjectError,
    OpenProjectHTTPError,
    OpenProjectNotFoundError,
)
from tests.fakes.openproject import FakeOpenProject

BASE_URL = "https://op.example.com"
TOKEN = "test-token"


async def _noop_sleep(delay: float) -> None:
    return None


def make_client(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_attempts: int = 5,
    sleep: Callable[[float], Awaitable[None]] = _noop_sleep,
) -> OpenProjectClient:
    return OpenProjectClient(
        BASE_URL,
        TOKEN,
        transport=transport,
        max_attempts=max_attempts,
        sleep=sleep,
    )


@respx.mock
async def test_get_json_sends_basic_auth_and_accept_header() -> None:
    route = respx.get(f"{BASE_URL}/api/v3/statuses").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = make_client()

    assert await client.get_json("/api/v3/statuses") == {"ok": True}
    await client.aclose()

    request = route.calls[0].request
    expected = base64.b64encode(f"apikey:{TOKEN}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.headers["Accept"] == "application/hal+json"


@respx.mock
async def test_retries_transient_5xx_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/api/v3/statuses").mock(
        side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = make_client(sleep=sleep)
    assert await client.get_json("/api/v3/statuses") == {"ok": True}
    await client.aclose()

    assert route.call_count == 2
    assert len(delays) == 1
    assert 0 < delays[0] <= 1.0


@respx.mock
async def test_honours_retry_after_header() -> None:
    respx.get(f"{BASE_URL}/api/v3/statuses").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}, text="slow down"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = make_client(sleep=sleep)
    await client.get_json("/api/v3/statuses")
    await client.aclose()

    assert delays == [3.0]


@respx.mock
async def test_retries_network_errors() -> None:
    route = respx.get(f"{BASE_URL}/api/v3/statuses").mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = make_client()

    assert await client.get_json("/api/v3/statuses") == {"ok": True}
    await client.aclose()
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_max_attempts() -> None:
    route = respx.get(f"{BASE_URL}/api/v3/statuses").mock(
        return_value=httpx.Response(500, text="boom")
    )
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = make_client(max_attempts=3, sleep=sleep)

    with pytest.raises(OpenProjectHTTPError) as excinfo:
        await client.get_json("/api/v3/statuses")
    await client.aclose()

    assert excinfo.value.status_code == 500
    assert "after" not in str(excinfo.value)  # HTTP errors do not claim retry counts
    assert "HTTP 500" in str(excinfo.value)
    assert route.call_count == 3
    assert len(delays) == 2


@respx.mock
async def test_auth_errors_are_not_retried() -> None:
    route = respx.get(f"{BASE_URL}/api/v3/users").mock(
        return_value=httpx.Response(
            403, json={"message": "You are not authorized to access this resource."}
        )
    )
    client = make_client()

    with pytest.raises(OpenProjectAuthError) as excinfo:
        await client.get_json("/api/v3/users")
    await client.aclose()

    assert excinfo.value.status_code == 403
    assert "not authorized" in str(excinfo.value)
    assert route.call_count == 1


@respx.mock
async def test_not_found_is_specific() -> None:
    respx.get(f"{BASE_URL}/api/v3/projects/nope").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    client = make_client()

    with pytest.raises(OpenProjectNotFoundError):
        await client.get_json("/api/v3/projects/nope")
    await client.aclose()


@respx.mock
async def test_follows_redirects() -> None:
    respx.get(f"{BASE_URL}/api/v3/work_packages/39/relations").mock(
        return_value=httpx.Response(
            308,
            headers={"Location": f"{BASE_URL}/api/v3/relations?filters=[]"},
        )
    )
    respx.get(f"{BASE_URL}/api/v3/relations", params={"filters": "[]"}).mock(
        return_value=httpx.Response(200, json={"total": 0})
    )
    client = make_client()

    assert await client.get_json("/api/v3/work_packages/39/relations") == {"total": 0}
    await client.aclose()


async def test_pagination_follows_next_by_offset() -> None:
    fake = FakeOpenProject(
        statuses=[{"id": index, "name": f"Status {index}"} for index in range(1, 6)],
        max_page_size=2,
    )
    client = make_client(transport=fake.transport)

    elements = await client.get_collection("/api/v3/statuses")
    await client.aclose()

    assert [element["id"] for element in elements] == [1, 2, 3, 4, 5]
    offsets = [request.url.params.get("offset", "1") for request in fake.requests]
    assert offsets == ["1", "2", "3"]


async def test_malformed_collection_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0})

    client = make_client(transport=httpx.MockTransport(handler))
    with pytest.raises(OpenProjectError, match=r"missing _embedded\.elements"):
        await client.get_collection("/api/v3/statuses")
    await client.aclose()


def test_rejects_empty_credentials() -> None:
    with pytest.raises(ValueError):
        OpenProjectClient("", TOKEN)
    with pytest.raises(ValueError):
        OpenProjectClient(BASE_URL, "")
