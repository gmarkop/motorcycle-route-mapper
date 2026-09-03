"""The shared Overpass gateway.

These exist because of a real failure: three layers queried Overpass at once,
the public instance grants about two slots per IP, and the third came back 429.
The rider saw "Points of interest unavailable (HTTPStatusError)" — a message
that named a Python class and explained nothing.
"""

import asyncio

import httpx
import pytest

from moto_route.config import Settings
from moto_route.services import overpass


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(overpass, "RETRY_BASE_DELAY", 0)


@pytest.fixture
def settings() -> Settings:
    return Settings(overpass_concurrency=1)


# ------------------------------------------------------------- the slot budget

async def test_queries_never_exceed_the_configured_concurrency(settings):
    """The bug in one test: without this, three layers race and one loses."""
    peak = 0
    inflight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal peak, inflight
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)          # long enough for the others to pile up
        inflight -= 1
        return httpx.Response(200, json={"elements": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await asyncio.gather(*(
            overpass.run_query(f"query {i}", settings, client) for i in range(5)
        ))

    assert peak == 1, f"{peak} queries were in flight at once, budget is 1"


async def test_a_larger_budget_is_honoured():
    """A self-hosted Overpass can take more, and the setting should mean it."""
    settings = Settings(overpass_concurrency=3)
    peak = 0
    inflight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal peak, inflight
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(
            overpass.run_query(f"q{i}", settings, client) for i in range(6)
        ))

    assert 1 < peak <= 3


# -------------------------------------------------------------------- retrying

async def test_a_throttled_query_is_retried_and_succeeds(settings):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"elements": [{"type": "node"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await overpass.run_query("q", settings, client)

    assert attempts == 2
    assert result["elements"]


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_transient_statuses_are_retried(settings, status):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError):
            await overpass.run_query("q", settings, client, attempts=3)

    assert attempts == 3


def test_a_malformed_query_is_not_retried(settings):
    """400 means the query is wrong; repeating it only adds load."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="parse error")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await overpass.run_query("bad", settings, client, attempts=3)

    with pytest.raises(overpass.OverpassError) as caught:
        asyncio.run(run())

    assert attempts == 1
    assert caught.value.status == 400


async def test_retry_after_is_respected(settings, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(overpass, "RETRY_BASE_DELAY", 1.0)
    monkeypatch.setattr(overpass.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"}, text="wait")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError):
            await overpass.run_query("q", settings, client, attempts=2)

    assert slept and slept[0] == 7.0, f"should honour Retry-After, slept {slept}"


# ------------------------------------------------------------------- messages

@pytest.mark.parametrize("status,expected", [
    (429, "rate-limiting"),
    (504, "timed out"),
    (503, "overloaded"),
    (400, "malformed"),
])
def test_statuses_are_described_in_plain_english(status, expected):
    assert expected in overpass.describe(status).lower()
    assert str(status) in overpass.describe(status)


async def test_an_unreachable_server_says_so(settings):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError, match="Could not reach Overpass"):
            await overpass.run_query("q", settings, client, attempts=1)


async def test_non_json_is_rejected_clearly(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError, match="not JSON"):
            await overpass.run_query("q", settings, client)
