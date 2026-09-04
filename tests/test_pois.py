"""Fuel/cafe/viewpoint tests, and the range planning that makes them useful."""

import httpx
import pytest

from moto_route.config import Settings
from moto_route.models import GeoPoint, Route
from moto_route.services import overpass, pois
from moto_route.services.cache import TTLCache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path, tank_range_km=250.0, fuel_reserve_fraction=0.15)


@pytest.fixture
def cache(tmp_path) -> TTLCache:
    return TTLCache(tmp_path, "poi-test")


def route_fixture() -> Route:
    return Route(name="Test", lines=[[GeoPoint(lat=48.0 + i * 0.001, lon=11.0)
                                      for i in range(1000)]])


@pytest.fixture
def route() -> Route:
    # ~111 km due north, so distances along the route are easy to reason about.
    return Route(name="Test", lines=[[GeoPoint(lat=48.0 + i * 0.001, lon=11.0)
                                      for i in range(1000)]])


def responder(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return handler


# ------------------------------------------------------------- range planning

def test_plan_takes_the_furthest_reachable_station():
    """Greedy on purpose: stopping early wastes time, so ride to the last pump
    still in range."""
    plan = pois.plan_fuel_stops(
        [80_000, 160_000, 240_000, 320_000, 400_000, 480_000],
        route_length_m=500_000, range_m=250_000,
    )
    # 212.5 km usable: 160 is the furthest reachable from 0, then 320 from 160.
    assert plan.stops == [1, 3]
    assert plan.reachable is True
    assert plan.gaps == []


def test_reserve_is_held_back():
    """A station exactly at the full range is not reachable on the reserve."""
    without_reserve = pois.plan_fuel_stops(
        [240_000], route_length_m=300_000, range_m=250_000, reserve_fraction=0.0)
    with_reserve = pois.plan_fuel_stops(
        [240_000], route_length_m=300_000, range_m=250_000, reserve_fraction=0.15)

    assert without_reserve.reachable is True
    assert with_reserve.reachable is False, "212 km of usable range cannot reach km 240"


def test_a_gap_longer_than_the_tank_is_reported():
    plan = pois.plan_fuel_stops(
        [50_000, 400_000], route_length_m=500_000, range_m=250_000)

    assert plan.reachable is False
    assert len(plan.gaps) == 1
    gap = plan.gaps[0]
    assert gap["from_km"] == 50.0
    assert gap["to_km"] == 400.0
    assert gap["length_km"] == 350.0


def test_planning_continues_past_a_gap():
    """A rider may carry a can; the rest of the route is still worth planning."""
    plan = pois.plan_fuel_stops(
        [400_000, 600_000], route_length_m=800_000, range_m=250_000)

    assert plan.reachable is False
    assert 0 in plan.stops and 1 in plan.stops


def test_a_route_with_no_stations_reports_one_whole_gap():
    plan = pois.plan_fuel_stops([], route_length_m=500_000, range_m=250_000)

    assert plan.reachable is False
    assert plan.gaps == [{"from_km": 0.0, "to_km": 500.0, "length_km": 500.0}]
    assert plan.stops == []


def test_a_route_inside_one_tank_needs_no_stop():
    plan = pois.plan_fuel_stops([50_000], route_length_m=100_000, range_m=250_000)

    assert plan.stops == []
    assert plan.gaps == []
    assert plan.reachable is True


def test_stations_behind_the_rider_are_ignored():
    """Sorting is by distance along the route, so a pump already passed at km 10
    must never be offered as the answer to running dry at km 300."""
    plan = pois.plan_fuel_stops(
        [10_000, 200_000, 5_000], route_length_m=400_000, range_m=250_000)

    chosen = sorted(plan.stops)
    assert 1 in chosen  # the km 200 station
    assert plan.reachable is True


def test_planning_terminates_on_pathological_input():
    """Duplicated and out-of-range distances must not spin the greedy loop."""
    plan = pois.plan_fuel_stops(
        [100_000] * 50 + [-5_000, 900_000],
        route_length_m=500_000, range_m=250_000,
    )
    assert isinstance(plan.stops, list)


# --------------------------------------------------------------- query + parse

def test_query_covers_all_three_categories_at_the_widest_corridor(settings):
    """Walking the corridor is what Overpass charges for, so it is walked once
    at the widest radius. The per-category corridor is applied afterwards."""
    query = pois.build_query([(48.0, 11.0), (48.1, 11.0)], settings)

    assert "fuel" in query and "cafe" in query
    assert '"tourism"="viewpoint"' in query

    widest = max(settings.fuel_corridor_m, settings.cafe_corridor_m,
                 settings.viewpoint_corridor_m)
    assert f"around:{int(widest)}," in query
    assert f"around:{int(settings.cafe_corridor_m)}," not in query, \
        "a second, narrower corridor walk is the cost this avoids"


async def test_the_narrow_corridors_are_still_enforced(settings, cache):
    """The behaviour that must survive the query change.

    Fuel is worth a detour and a cafe is not, so a cafe a kilometre off the
    route must still be dropped even though the query now asks for everything
    within fuel's radius.
    """
    payload = {"elements": [
        # Both ~700 m east of a route running due north along lon 11.0.
        {"type": "node", "id": 1, "lat": 48.05, "lon": 11.0094,
         "tags": {"amenity": "fuel", "name": "Far pump"}},
        {"type": "node", "id": 2, "lat": 48.05, "lon": 11.0094,
         "tags": {"amenity": "cafe", "name": "Far cafe"}},
    ]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await pois.find_pois(route_fixture(), settings, client, cache)

    names = {p["name"] for p in result["pois"]}
    assert "Far pump" in names, "fuel at 700 m is within its 1 km corridor"
    assert "Far cafe" not in names, "a cafe at 700 m is well outside its 300 m corridor"


def test_query_uses_nwr_so_mapped_areas_are_not_missed(settings):
    """A motorway services is usually a way or relation, not a node."""
    query = pois.build_query([(48.0, 11.0), (48.1, 11.0)], settings)
    assert query.count("nwr(") == 2, "one corridor walk per tag key, not per category"
    assert "out center" in query


def test_the_corridor_is_walked_as_few_times_as_possible(settings):
    """The regression that mattered: a 389 km route timed out because the
    corridor was walked once per tag filter."""
    coords = [(38.0 + i * 0.01, 22.0) for i in range(169)]
    assert pois.build_query(coords, settings).count("around:") == 2


async def test_pois_are_placed_along_the_route(route, settings, cache):
    payload = {"elements": [
        {"type": "node", "id": 1, "lat": 48.5, "lon": 11.0,
         "tags": {"amenity": "fuel", "name": "Aral", "brand": "Aral",
                  "opening_hours": "24/7"}},
        {"type": "node", "id": 2, "lat": 48.2, "lon": 11.0,
         "tags": {"amenity": "cafe", "name": "Cafe Alpin"}},
    ]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await pois.find_pois(route, settings, client, cache)

    assert result["available"] is True
    assert result["counts"] == {"fuel": 1, "cafe": 1, "viewpoint": 0}

    # Sorted by position along the route, so the cafe at 48.2 comes first.
    assert [p["category"] for p in result["pois"]] == ["cafe", "fuel"]
    assert result["pois"][0]["distance_along_route_m"] > 20_000
    assert "opening_hours: 24/7" in result["pois"][1]["detail"]


async def test_a_way_is_located_from_its_overpass_centroid(route, settings, cache):
    payload = {"elements": [
        {"type": "way", "id": 7, "center": {"lat": 48.3, "lon": 11.0},
         "tags": {"amenity": "fuel", "name": "Autohof"}},
    ]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await pois.find_pois(route, settings, client, cache)

    assert len(result["pois"]) == 1
    assert result["pois"][0]["osm_url"].endswith("/way/7")


async def test_a_poi_far_off_the_route_is_discarded(route, settings, cache):
    payload = {"elements": [
        {"type": "node", "id": 3, "lat": 48.5, "lon": 12.0,  # ~74 km east
         "tags": {"amenity": "fuel"}},
    ]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await pois.find_pois(route, settings, client, cache)

    assert result["pois"] == []


async def test_unnamed_pois_still_get_a_usable_label(route, settings, cache):
    payload = {"elements": [
        {"type": "node", "id": 4, "lat": 48.4, "lon": 11.0, "tags": {"amenity": "fuel"}},
    ]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await pois.find_pois(route, settings, client, cache)

    assert result["pois"][0]["name"] == "Fuel station"


async def test_recommended_stops_are_flagged_on_the_fuel_stations(settings, cache):
    """End to end: a long route with sparse fuel must mark which pumps to use."""
    long_route = Route(name="Long", lines=[[GeoPoint(lat=45.0 + i * 0.001, lon=11.0)
                                            for i in range(4000)]])  # ~445 km
    payload = {"elements": [
        {"type": "node", "id": 10, "lat": 45.5, "lon": 11.0, "tags": {"amenity": "fuel"}},
        {"type": "node", "id": 11, "lat": 46.0, "lon": 11.0, "tags": {"amenity": "fuel"}},
        {"type": "node", "id": 12, "lat": 48.0, "lon": 11.0, "tags": {"amenity": "fuel"}},
    ]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        result = await pois.find_pois(long_route, settings, client, cache, tank_range_km=150)

    recommended = [p for p in result["pois"] if p.get("recommended")]
    assert recommended, "a 445 km route on a 150 km tank must require a stop"
    assert result["fuel_plan"]["usable_range_km"] == pytest.approx(127.5)


async def test_tank_range_from_the_request_overrides_the_default(settings, cache):
    long_route = Route(name="Long", lines=[[GeoPoint(lat=45.0 + i * 0.001, lon=11.0)
                                            for i in range(4000)]])
    payload = {"elements": []}

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(payload))) as client:
        small = await pois.find_pois(long_route, settings, client, cache, tank_range_km=100)

    assert small["fuel_plan"]["usable_range_km"] == pytest.approx(85.0)
    assert small["fuel_plan"]["reachable"] is False


async def test_offline_mode_makes_no_request(route, settings, cache):
    settings.offline = True

    def handler(request):
        raise AssertionError("offline mode must not touch the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await pois.find_pois(route, settings, client, cache)

    assert result["available"] is False


async def test_rate_limiting_is_reported_in_plain_english(monkeypatch, route, settings, cache):
    """429 is the failure a rider will actually hit, so it must explain itself."""
    monkeypatch.setattr(overpass, "RETRY_BASE_DELAY", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await pois.find_pois(route, settings, client, cache)

    assert result["available"] is False
    assert result["pois"] == []
    assert "429" in result["reason"]
    assert "rate-limiting" in result["reason"]
