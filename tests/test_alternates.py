"""Alternate-route tests against a mocked OSRM."""

import httpx
import pytest

from moto_route.config import Settings
from moto_route.models import GeoPoint, Route
from moto_route.services import alternates
from moto_route.services.cache import TTLCache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path)


@pytest.fixture
def cache(tmp_path) -> TTLCache:
    return TTLCache(tmp_path, "routing-test")


@pytest.fixture
def route() -> Route:
    return Route(name="A to B", lines=[[GeoPoint(lat=48.0 + i * 0.01, lon=11.0)
                                        for i in range(50)]])


def osrm_response(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "Ok", "routes": routes})
    return handler


def straight_geometry(count=40):
    # GeoJSON order: [longitude, latitude].
    return {"coordinates": [[11.0, 48.0 + i * 0.01] for i in range(count)]}


def twisty_geometry(count=40):
    return {"coordinates": [[11.0 + (0.004 if i % 2 else -0.004), 48.0 + i * 0.005]
                            for i in range(count)]}


async def test_alternates_are_labelled_by_what_makes_them_worth_taking(route, settings, cache):
    handler = osrm_response([
        {"distance": 55000, "duration": 3000, "geometry": straight_geometry()},
        {"distance": 62000, "duration": 4200, "geometry": twisty_geometry()},
    ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await alternates.find_alternates(route, settings, client, cache)

    labels = [a["label"] for a in result["alternates"]]
    assert "Fastest" in labels
    assert "Most corners" in labels


async def test_the_curvy_option_really_does_score_curvier(route, settings, cache):
    handler = osrm_response([
        {"distance": 55000, "duration": 3000, "geometry": straight_geometry()},
        {"distance": 62000, "duration": 4200, "geometry": twisty_geometry()},
    ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await alternates.find_alternates(route, settings, client, cache)

    by_label = {a["label"]: a for a in result["alternates"]}
    assert by_label["Most corners"]["curviness"] > by_label["Fastest"]["curviness"] * 5


async def test_coordinates_are_flipped_into_leaflet_order(route, settings, cache):
    handler = osrm_response([{"distance": 1000, "duration": 60,
                              "geometry": straight_geometry(3)}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await alternates.find_alternates(route, settings, client, cache)

    first = result["alternates"][0]["coordinates"][0]
    assert first[0] == pytest.approx(48.0)   # latitude first for Leaflet
    assert first[1] == pytest.approx(11.0)


async def test_the_original_route_is_measured_for_comparison(route, settings, cache):
    handler = osrm_response([{"distance": 1000, "duration": 60,
                              "geometry": straight_geometry(3)}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await alternates.find_alternates(route, settings, client, cache)

    original = result["original"]
    assert original["is_original"] is True
    assert original["distance_km"] == pytest.approx(route.distance_m / 1000, rel=0.01)


async def test_a_loop_is_reported_rather_than_routed(settings, cache):
    """Start == finish makes 'route me from A to A' meaningless, not an error."""
    loop_points = (
        [GeoPoint(lat=48.0 + i * 0.01, lon=11.0) for i in range(20)]
        + [GeoPoint(lat=48.19 - i * 0.01, lon=11.001) for i in range(20)]
    )
    loop = Route(name="Loop", lines=[loop_points])

    def handler(request):
        raise AssertionError("a loop must not be sent to the router")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await alternates.find_alternates(loop, settings, client, cache)

    assert result["available"] is False
    assert "loop" in result["reason"].lower()


async def test_a_router_error_code_is_surfaced(route, settings, cache):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "NoRoute", "routes": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await alternates.find_alternates(route, settings, client, cache)

    assert result["available"] is False
    assert "NoRoute" in result["reason"]


async def test_a_network_failure_degrades_gracefully(route, settings, cache):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await alternates.find_alternates(route, settings, client, cache)

    assert result["available"] is False
    assert result["alternates"] == []
