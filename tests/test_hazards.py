"""Hazard service tests, with Overpass answered by a mock transport."""

import httpx
import pytest

from moto_route.config import Settings
from moto_route.models import GeoPoint, Route
from moto_route.services import hazards, overpass
from moto_route.services.cache import TTLCache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path, hazard_corridor_m=150)


@pytest.fixture
def cache(tmp_path) -> TTLCache:
    return TTLCache(tmp_path, "hazard-test")


@pytest.fixture
def route() -> Route:
    # ~11 km due north from 48.0/11.0.
    return Route(name="Test", lines=[[GeoPoint(lat=48.0 + i * 0.001, lon=11.0)
                                      for i in range(100)]])


def responder(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return handler


# ------------------------------------------------------------- query building

def test_query_uses_an_around_corridor_not_a_bounding_box():
    coords = [(48.0, 11.0), (48.1, 11.0), (48.2, 11.1)]
    query = hazards.build_query(coords, radius_m=150, limit=50)

    assert "around:150," in query
    assert "48.00000,11.00000" in query
    assert "[out:json]" in query and "out geom 50;" in query


def test_both_query_shapes_look_for_the_same_things():
    """The shapes trade spatial passes against intermediate set size.

    `filtered` runs each tag filter inside its own `around` — eight passes, but
    each answered from the tag index. `grouped` walks the corridor twice and
    filters named sets — fewer passes, but it materialises every road in the
    corridor first, which through a town times out. Whichever is in use, the
    same tags must be searched.
    """
    coords = [(38.0 + i * 0.01, 22.0) for i in range(20)]
    filtered = hazards.build_query(coords, 150, 200, style="filtered")
    grouped = hazards.build_query(coords, 150, 200, style="grouped")

    assert filtered.count("around:") == 8
    assert grouped.count("around:") == 2
    assert "->.roads;" in grouped and "->.gates;" in grouped

    for tag in ('"highway"="construction"', '"construction"', '"access"="no"',
                '"motor_vehicle"="no"', '"seasonal"="yes"', '"snowplowing"="no"',
                '"barrier"="lift_gate"'):
        assert tag in filtered, f"{tag} missing from filtered"
        assert tag in grouped, f"{tag} missing from grouped"


def test_the_default_shape_is_the_one_with_evidence():
    """`grouped` failed a chunk on a real route that `filtered` had answered
    whole. Until that is measured the other way round, filtered is the default."""
    assert Settings().overpass_query_style == "filtered"

    coords = [(38.0 + i * 0.01, 22.0) for i in range(20)]
    default = hazards.build_query(coords, 150, 200)
    assert default.count("around:") == 8


def test_query_covers_construction_gates_and_access_restrictions():
    query = hazards.build_query([(48.0, 11.0), (48.1, 11.0)], 150, 50)

    assert '"highway"="construction"' in query
    assert '"access"="no"' in query
    assert '"motor_vehicle"="no"' in query
    assert '"barrier"' in query
    assert "out geom" in query


def test_long_routes_are_thinned_before_querying():
    # 5000 points would make an Overpass URL absurdly long.
    dense = [(48.0 + i * 0.0002, 11.0) for i in range(5000)]
    coords = hazards._query_coordinates(dense)

    assert len(coords) <= hazards._MAX_QUERY_POINTS
    assert coords[0] == dense[0]


# ------------------------------------------------------------ result handling

async def test_a_closure_on_the_route_is_reported(route, settings, cache):
    payload = {"elements": [{
        "type": "way", "id": 42,
        "tags": {"highway": "construction", "construction": "secondary", "name": "L100"},
        "geometry": [{"lat": 48.05, "lon": 11.0}, {"lat": 48.06, "lon": 11.0}],
    }]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    assert result["available"] is True
    assert len(result["hazards"]) == 1

    hazard = result["hazards"][0]
    assert hazard["category"] == "construction"
    assert hazard["severity"] == "closed"
    assert "secondary" in hazard["label"]
    assert "L100" in hazard["detail"]
    assert hazard["osm_url"].endswith("/way/42")


async def test_distance_along_the_route_is_computed(route, settings, cache):
    """A pin is only useful once it says 'at km 5' rather than just 'somewhere'."""
    payload = {"elements": [{
        "type": "way", "id": 1, "tags": {"highway": "construction"},
        "geometry": [{"lat": 48.05, "lon": 11.0}],
    }]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    # 48.05 is halfway along a route running 48.000 -> 48.099.
    along_km = result["hazards"][0]["distance_along_route_m"] / 1000
    assert 5.0 < along_km < 6.0
    assert result["hazards"][0]["distance_off_route_m"] < 50


async def test_a_hazard_far_from_the_route_is_discarded(route, settings, cache):
    """Overpass measures against our simplified query line; re-check the real one."""
    payload = {"elements": [{
        "type": "way", "id": 2, "tags": {"highway": "construction"},
        "geometry": [{"lat": 48.05, "lon": 11.5}],  # ~37 km east of the route
    }]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    assert result["hazards"] == []


async def test_hazards_come_back_ordered_by_position_along_the_route(route, settings, cache):
    payload = {"elements": [
        {"type": "node", "id": 3, "lat": 48.08, "lon": 11.0,
         "tags": {"barrier": "gate", "access": "no"}},
        {"type": "node", "id": 4, "lat": 48.02, "lon": 11.0,
         "tags": {"barrier": "lift_gate"}},
    ]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    positions = [h["distance_along_route_m"] for h in result["hazards"]]
    assert positions == sorted(positions)
    assert result["hazards"][0]["osm_id"] == 4


def test_tag_classification_separates_severity_levels():
    assert hazards._classify({"highway": "construction"})[2] == "closed"
    assert hazards._classify({"highway": "residential", "motor_vehicle": "no"})[2] == "closed"
    assert hazards._classify({"barrier": "lift_gate"})[2] == "restricted"
    assert hazards._classify({"highway": "tertiary", "seasonal": "yes"})[2] == "info"


def test_long_ways_are_sampled_rather_than_sent_whole():
    element = {"type": "way", "geometry": [{"lat": 48.0 + i * 0.001, "lon": 11.0}
                                           for i in range(500)]}
    assert len(hazards._element_geometry(element)) <= hazards._MAX_WAY_VERTICES


async def test_offline_mode_reports_unavailable(route, settings, cache):
    settings.offline = True

    def handler(request):
        raise AssertionError("offline mode must not touch the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    assert result["available"] is False


async def test_an_overpass_failure_names_the_status_code(monkeypatch, route, settings, cache):
    """"HTTPStatusError" told a rider nothing. The status is the useful fact."""
    monkeypatch.setattr(overpass, "RETRY_BASE_DELAY", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="gateway timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    assert result["available"] is False
    assert result["hazards"] == []
    assert "504" in result["reason"]
    assert "timed out" in result["reason"].lower()


async def test_results_are_cached_between_calls(route, settings, cache):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await hazards.find_hazards(route, settings, client, cache)
        await hazards.find_hazards(route, settings, client, cache)

    assert calls["n"] == 1


async def test_a_partial_answer_is_cached_only_briefly(monkeypatch, route, settings, cache):
    """The panel says "press Refresh to try the rest" — so Refresh must retry.

    A partial answer cached for the full six hours would make that instruction
    a lie: every Refresh would replay the same gaps out of the cache without
    ever asking Overpass again.
    """
    monkeypatch.setattr(overpass, "RETRY_BASE_DELAY", 0)
    settings = Settings(cache_dir=settings.cache_dir, hazard_corridor_m=150,
                        overpass_max_points=10)
    # A zig-zag, because Douglas-Peucker collapses the straight fixture route to
    # two points and two points are always a single chunk.
    route = Route(name="Zig", lines=[[GeoPoint(lat=48.0 + i * 0.004,
                                               lon=11.0 + (i % 2) * 0.02)
                                      for i in range(40)]])

    ttls: list[int] = []
    original = cache.set
    cache.set = lambda key, value, ttl_s: (ttls.append(ttl_s), original(key, value, ttl_s))[1]

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # The first chunk fails all three of its attempts; the rest answer.
        if calls["n"] <= 3:
            return httpx.Response(504, text="gateway timeout")
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    assert result["partial"] is True
    assert ttls == [settings.partial_ttl_s]
    assert settings.partial_ttl_s < settings.hazard_ttl_s


async def test_a_complete_answer_keeps_the_long_cache_life(route, settings, cache):
    """The short TTL is for gaps only; a whole answer still lasts six hours."""
    ttls: list[int] = []
    original = cache.set
    cache.set = lambda key, value, ttl_s: (ttls.append(ttl_s), original(key, value, ttl_s))[1]

    async with httpx.AsyncClient(
            transport=httpx.MockTransport(responder({"elements": []}))) as client:
        result = await hazards.find_hazards(route, settings, client, cache)

    assert result["partial"] is False
    assert ttls == [settings.hazard_ttl_s]
