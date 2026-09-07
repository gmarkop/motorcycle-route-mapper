"""Elevation service tests, with the terrain model answered by a mock transport."""

import httpx
import pytest

from moto_route.config import Settings
from moto_route.services import elevation
from moto_route.services.cache import TTLCache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path, elevation_sample_m=100.0)


@pytest.fixture
def cache(tmp_path) -> TTLCache:
    return TTLCache(tmp_path, "elevation-test")


@pytest.fixture
def climb():
    """~11 km due north, climbing steadily."""
    return [(48.0 + i * 0.001, 11.0) for i in range(100)]


def responder(values):
    def handler(request: httpx.Request) -> httpx.Response:
        n = len(request.url.params["latitude"].split(","))
        out = values if values is not None else [100.0 + i for i in range(n)]
        return httpx.Response(200, json={"elevation": out[:n]})
    return handler


# ---------------------------------------------------------------- gradients

def test_a_level_road_has_no_gradient():
    assert elevation.gradients([(0, 500), (100, 500), (200, 500)]) == [0.0, 0.0, 0.0]


def test_a_climb_is_positive_and_a_descent_negative():
    up = elevation.gradients([(0, 0), (100, 10), (200, 20)])
    assert all(g > 0 for g in up), up
    down = elevation.gradients([(0, 20), (100, 10), (200, 0)])
    assert all(g < 0 for g in down), down


def test_a_ten_percent_climb_reads_as_ten_percent():
    # 100 m along, 10 m up, measured centred over the middle sample.
    assert elevation.gradients([(0, 0), (100, 10), (200, 20)])[1] == pytest.approx(10.0)


def test_the_ends_get_a_one_sided_answer_rather_than_none():
    slopes = elevation.gradients([(0, 0), (100, 10), (200, 20)])
    assert len(slopes) == 3
    assert slopes[0] != 0.0 and slopes[-1] != 0.0


def test_repeated_distances_do_not_divide_by_zero():
    """Two samples at the same distance happen on a doubled-back track."""
    assert elevation.gradients([(0, 500), (0, 520), (0, 500)]) == [0.0, 0.0, 0.0]


def test_a_single_sample_is_flat_not_an_error():
    assert elevation.gradients([(0, 500)]) == [0.0]
    assert elevation.gradients([]) == []


# ------------------------------------------------------------------ lookup

async def test_the_file_is_used_when_it_has_heights(settings, cache, climb):
    def handler(request):
        raise AssertionError("must not ask the model when the file knows")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await elevation.profile(climb, [500.0 + i for i in range(len(climb))],
                                         settings, client, cache)

    assert result["available"] is True
    assert result["source"] == "file"


async def test_the_model_fills_in_when_the_file_is_silent(settings, cache, climb):
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(None))) as client:
        result = await elevation.profile(climb, [None] * len(climb),
                                         settings, client, cache)

    assert result["available"] is True
    assert result["source"] == "dem"
    assert result["samples"] and all("gradient_pct" in s for s in result["samples"])
    assert "90 m" in result["note"]


async def test_a_partly_empty_file_still_uses_the_model(settings, cache, climb):
    """One missing height makes the file's profile a lie, not a small gap."""
    partial = [500.0] * len(climb)
    partial[len(climb) // 2] = None

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(None))) as client:
        result = await elevation.profile(climb, partial, settings, client, cache)

    assert result["source"] == "dem"


async def test_a_short_result_is_an_error_not_a_silent_misalignment(settings, cache, climb):
    """This is the failure that would be worst if swallowed.

    Fewer values than coordinates would shift every height onto the wrong
    point, so the profile would look plausible and be wrong from the gap
    onwards. Better to say the lookup failed.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elevation": [100.0, 200.0]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await elevation.profile(climb, [None] * len(climb),
                                         settings, client, cache)

    assert result["available"] is False
    assert "coordinates" in result["reason"]


async def test_an_unreachable_service_is_reported_not_raised(settings, cache, climb):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await elevation.profile(climb, [None] * len(climb),
                                         settings, client, cache)

    assert result["available"] is False
    assert "elevation service" in result["reason"]


async def test_results_are_cached_between_calls(settings, cache, climb):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        n = len(request.url.params["latitude"].split(","))
        return httpx.Response(200, json={"elevation": [100.0] * n})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await elevation.profile(climb, [None] * len(climb), settings, client, cache)
        first = calls["n"]
        await elevation.profile(climb, [None] * len(climb), settings, client, cache)

    assert calls["n"] == first, "terrain does not move; it should be cached"


async def test_a_long_route_is_batched_within_the_api_limit(tmp_path, cache):
    """Open-Meteo takes 100 coordinates per request; ask for more and it fails."""
    settings = Settings(cache_dir=tmp_path, elevation_sample_m=10.0,
                        max_elevation_samples=250)
    long_route = [(48.0 + i * 0.0005, 11.0) for i in range(600)]
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(request.url.params["latitude"].split(","))
        sizes.append(n)
        return httpx.Response(200, json={"elevation": [100.0] * n})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await elevation.profile(long_route, [None] * len(long_route),
                                         settings, client, cache)

    assert result["available"] is True
    assert sizes and max(sizes) <= elevation.MAX_PER_REQUEST, sizes
    assert sum(sizes) <= settings.max_elevation_samples + 1


async def test_offline_mode_never_reaches_for_the_model(tmp_path, cache, climb):
    settings = Settings(cache_dir=tmp_path, offline=True)

    def handler(request):
        raise AssertionError("offline mode must not make requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await elevation.profile(climb, [None] * len(climb),
                                         settings, client, cache)

    assert result["available"] is False
    assert "offline" in result["reason"].lower()
