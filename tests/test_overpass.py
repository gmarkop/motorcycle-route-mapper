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


async def test_an_unreachable_server_names_the_host_and_the_reason(settings):
    """"ConnectError" is a class name. It does not say which server, or why."""
    def handler(request):
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError) as caught:
            await overpass.run_query("q", settings, client, attempts=1)

    message = str(caught.value)
    assert "overpass-api.de" in message
    assert "connection refused" in message
    assert "refuse connections when busy" in message


async def test_non_json_is_rejected_clearly(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError, match="not JSON"):
            await overpass.run_query("q", settings, client)


# ------------------------------------------------- the budget must be coherent

def test_the_declared_query_budget_never_exceeds_the_http_timeout():
    """The bug this guards against.

    The POI query told Overpass it could take 90 seconds while the HTTP client
    hung up after 20, so every genuinely slow query failed client-side and was
    reported as a connection timeout. The two numbers have to agree, and they
    now come from one setting — this fails if anyone splits them again.
    """
    for budget in (10, 60, 90, 180):
        settings = Settings(overpass_timeout_s=budget)
        declared = int(overpass.query_header(settings).split("timeout:")[1].split("]")[0])
        http_wait = settings.overpass_timeout_s + overpass.TRANSFER_MARGIN_S

        assert declared == budget
        assert http_wait > declared, (
            f"HTTP wait {http_wait}s must outlast the {declared}s the query is "
            "allowed, or a slow query can only ever fail"
        )


def test_every_query_builder_uses_the_shared_header():
    """One place decides the budget; a builder writing its own would drift."""
    from moto_route.services import hazards, pois

    settings = Settings(overpass_timeout_s=42)
    header = overpass.query_header(settings)
    coords = [(38.0, 23.7), (38.5, 22.5)]

    assert pois.build_query(coords, settings).startswith(header)
    assert hazards.build_query(coords, 150, 50, header=header).startswith(header)


async def test_a_timeout_explains_itself(settings):
    """"ConnectTimeout" is a class name; a rider needs to know what to do."""
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError) as caught:
            await overpass.run_query("q", settings, client, attempts=1)

    message = str(caught.value)
    assert "did not answer" in message
    assert "shorter route" in message



# ------------------------------------------------------------------- failover

async def test_a_refused_connection_falls_over_to_the_next_endpoint():
    """The failure a mirror actually fixes.

    The main instance drops connections per-query when it is busy, which is how
    the heavy closure query failed while the lighter POI query beside it
    succeeded, both against the same server.
    """
    settings = Settings(
        overpass_url="https://primary.example/api",
        overpass_fallback_urls=["https://mirror.example/api"],
    )
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == "primary.example":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"elements": [{"type": "node"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await overpass.run_query("q", settings, client)

    assert tried == ["primary.example", "mirror.example"]
    assert result["elements"]


async def test_attempts_rotate_through_the_endpoints():
    settings = Settings(
        overpass_url="https://a.example/api",
        overpass_fallback_urls=["https://b.example/api"],
    )
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError):
            await overpass.run_query("q", settings, client, attempts=3)

    assert tried == ["a.example", "b.example", "a.example"]


async def test_the_failure_lists_every_endpoint_tried():
    settings = Settings(
        overpass_url="https://a.example/api",
        overpass_fallback_urls=["https://b.example/api"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            raise httpx.ConnectError("refused")
        return httpx.Response(429, text="slow down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError) as caught:
            await overpass.run_query("q", settings, client, attempts=2)

    message = str(caught.value)
    assert "a.example" in message and "b.example" in message


async def test_fallbacks_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("MOTO_OVERPASS_FALLBACK_URLS", "")
    settings = Settings(overpass_url="https://only.example/api")
    assert settings.overpass_endpoints == ["https://only.example/api"]

    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError):
            await overpass.run_query("q", settings, client, attempts=3)

    assert set(tried) == {"only.example"}


def test_an_unset_list_still_gets_its_default(monkeypatch):
    """Empty means empty; unset means the default. Collapsing the two would
    make a non-empty default impossible to switch off."""
    monkeypatch.delenv("MOTO_OVERPASS_FALLBACK_URLS", raising=False)
    assert Settings().overpass_fallback_urls, "an unset variable keeps the default"

    monkeypatch.setenv("MOTO_OVERPASS_FALLBACK_URLS", "")
    assert Settings().overpass_fallback_urls == []


# ------------------------------------------------------------------- chunking

def test_chunks_overlap_so_the_corridor_has_no_seams():
    """Without a shared point the corridor has a hole at every join — exactly
    where one query stops and the next starts."""
    coords = [(38.0 + i * 0.01, 22.0) for i in range(169)]
    chunks = overpass.chunk_coordinates(coords, 60)

    assert [len(c) for c in chunks] == [60, 60, 51]
    for before, after in zip(chunks, chunks[1:]):
        assert before[-1] == after[0], "a seam with no shared point is a gap"

    rebuilt = chunks[0] + [p for c in chunks[1:] for p in c[1:]]
    assert rebuilt == coords, "chunking must not lose or reorder points"


def test_a_short_route_is_still_one_query():
    coords = [(38.0 + i * 0.01, 22.0) for i in range(44)]
    assert len(overpass.chunk_coordinates(coords, 60)) == 1


@pytest.mark.parametrize("size", [0, 1, 2])
def test_a_degenerate_chunk_size_cannot_loop_forever(size):
    coords = [(38.0 + i * 0.01, 22.0) for i in range(10)]
    chunks = overpass.chunk_coordinates(coords, size)
    assert chunks and all(len(c) >= 2 for c in chunks)


async def test_results_from_every_chunk_are_merged(settings):
    coords = [(38.0 + i * 0.01, 22.0) for i in range(169)]
    chunks = overpass.chunk_coordinates(coords, 60)
    served = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        return httpx.Response(200, json={"elements": [
            {"type": "way", "id": served * 10},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await overpass.run_chunked(chunks, lambda c: "q", settings, client)

    assert served == len(chunks)
    assert len(result["elements"]) == len(chunks)
    assert result["partial"] is False


async def test_a_feature_spanning_a_seam_is_not_duplicated(settings):
    """Both neighbouring queries return it; the rider should see one closure."""
    chunks = [[(38.0, 22.0)], [(38.1, 22.0)]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elements": [{"type": "way", "id": 7}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await overpass.run_chunked(chunks, lambda c: "q", settings, client)

    assert len(result["elements"]) == 1


async def test_one_failed_chunk_still_returns_the_rest(settings):
    """Most of a route's closures beats none, as long as we say which."""
    chunks = [[(38.0, 22.0)], [(38.1, 22.0)], [(38.2, 22.0)]]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(400, text="too complex")
        return httpx.Response(200, json={"elements": [{"type": "way", "id": calls}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await overpass.run_chunked(chunks, lambda c: "q", settings, client)

    assert result["partial"] is True
    assert result["failed_chunks"] == 1 and result["total_chunks"] == 3
    assert len(result["elements"]) == 2


async def test_every_chunk_failing_is_still_a_failure(settings):
    chunks = [[(38.0, 22.0)], [(38.1, 22.0)]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError):
            await overpass.run_chunked(chunks, lambda c: "q", settings, client)


# ------------------------------------------------------------- the time budget

async def test_a_layer_stops_when_its_budget_is_spent(monkeypatch):
    """One stubborn chunk spent 311s — three attempts at the full timeout —
    while the rider watched an empty panel. The budget caps the whole layer."""
    settings = Settings(overpass_concurrency=1, overpass_deadline_s=60.0)
    chunks = [[(38.0, 22.0)], [(38.1, 22.0)], [(38.2, 22.0)]]
    served = []
    clock = {"now": 1000.0}

    monkeypatch.setattr(overpass.time, "monotonic", lambda: clock["now"])

    def handler(request: httpx.Request) -> httpx.Response:
        served.append(1)
        clock["now"] += 35.0          # each chunk eats most of the budget
        return httpx.Response(200, json={"elements": [{"type": "way", "id": len(served)}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await overpass.run_chunked(chunks, lambda c: "q", settings, client)

    assert len(served) == 2, "the third chunk had no time left and was skipped"
    assert result["partial"] is True
    assert result["failed_chunks"] == 1
    assert len(result["elements"]) == 2


async def test_a_query_is_not_started_without_time_to_finish(monkeypatch):
    settings = Settings(overpass_concurrency=1)
    clock = {"now": 500.0}
    monkeypatch.setattr(overpass.time, "monotonic", lambda: clock["now"])

    def handler(request):
        raise AssertionError("must not start a query it cannot finish")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(overpass.OverpassError, match="Ran out of time"):
            await overpass.run_query("q", settings, client,
                                     deadline=clock["now"] + 5.0)


async def test_the_request_timeout_shrinks_to_what_is_left(monkeypatch):
    """A 105s timeout inside a 40s remaining budget would overshoot it."""
    settings = Settings(overpass_concurrency=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(overpass.time, "monotonic", lambda: clock["now"])
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await overpass.run_query("q", settings, client, deadline=40.0)

    assert seen and seen[0] is not None
    assert seen[0] <= 40.0, f"asked for {seen[0]}s inside a 40s budget"


async def test_without_a_deadline_the_full_timeout_is_used():
    settings = Settings(overpass_concurrency=1, overpass_timeout_s=90)
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await overpass.run_query("q", settings, client, deadline=None)

    assert seen[0] == 90 + overpass.TRANSFER_MARGIN_S


# ------------------------------------------------- chunks running concurrently

async def _peak_inflight(chunks, settings, delay=0.05):
    """Run a layer and report the most queries that were ever in flight."""
    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(delay)
        state["now"] -= 1
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await overpass.run_chunked(chunks, lambda c: "q", settings, client)
    return state["peak"], result


async def test_the_chunks_of_a_layer_run_at_the_same_time():
    """The point of the change: a 3-chunk route is no longer 3 round trips deep."""
    settings = Settings(overpass_concurrency=3)
    chunks = [[(38.0, 22.0)], [(38.1, 22.0)], [(38.2, 22.0)]]

    peak, result = await _peak_inflight(chunks, settings)

    assert peak == 3, f"chunks still serialised: peak was {peak}"
    assert result["partial"] is False


async def test_concurrent_chunks_still_obey_the_semaphore():
    """The safety property. Running chunks together must not ask the public
    servers for more slots than they grant — the semaphore, not the shape of
    the loop, is what decides how hard Overpass is hit."""
    settings = Settings(overpass_concurrency=2)
    chunks = [[(38.0 + i / 10, 22.0)] for i in range(6)]

    peak, _ = await _peak_inflight(chunks, settings)

    assert peak == 2, f"{peak} queries in flight at once, budget is 2"


async def test_a_slow_chunk_no_longer_delays_the_others():
    """What the fair-share allocation used to compensate for.

    Sequentially, a chunk that hangs consumes the budget its siblings needed.
    Concurrently it delays only itself, so the layer finishes in about the time
    of its slowest chunk rather than the sum of all of them.
    """
    settings = Settings(overpass_concurrency=4)
    chunks = [[(38.0 + i / 10, 22.0)] for i in range(4)]

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.20)
        return httpx.Response(200, json={"elements": []})

    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        started = loop.time()
        await overpass.run_chunked(chunks, lambda c: "q", settings, client)
        elapsed = loop.time() - started

    # Four 0.20s chunks: ~0.80s one after another, ~0.20s together.
    assert elapsed < 0.5, f"took {elapsed:.2f}s — chunks look sequential"


async def test_a_narrow_semaphore_shares_the_budget_between_chunks():
    """Concurrency alone does not make one slow chunk harmless.

    Where the semaphore is narrower than the number of chunks they still take
    turns, so each is capped at its share of the budget — the fair-share rule
    the sequential loop used, generalised. Otherwise one chunk's retries spend
    the time the others were queueing for.
    """
    settings = Settings(overpass_concurrency=1, overpass_deadline_s=120.0,
                        overpass_timeout_s=90)
    chunks = [[(38.0 + i / 10, 22.0)] for i in range(4)]
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await overpass.run_chunked(chunks, lambda c: "q", settings, client)

    # Four chunks, one at a time: four turns, so 30s each — not the 105s a
    # single query would otherwise be allowed.
    assert seen[0] == pytest.approx(30.0, abs=1.0), f"first chunk got {seen[0]}s"


async def test_a_wide_semaphore_gives_each_chunk_the_whole_budget():
    """With a slot per chunk nothing is queueing, so nothing needs rationing."""
    settings = Settings(overpass_concurrency=4, overpass_deadline_s=120.0,
                        overpass_timeout_s=90)
    chunks = [[(38.0 + i / 10, 22.0)] for i in range(4)]
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await overpass.run_chunked(chunks, lambda c: "q", settings, client)

    # One turn, so each chunk may use the full per-query timeout.
    assert seen[0] == pytest.approx(90 + overpass.TRANSFER_MARGIN_S, abs=1.0)
