"""Incident provider tests.

The live endpoints were unreachable from the environment this was written in,
so these run against recorded response shapes. That makes them a test of the
mapping and the failure handling, not proof that the upstream schema is what we
think it is — which is exactly why every provider fails soft.
"""

import httpx
import pytest

from moto_route.config import Settings
from moto_route.models import GeoPoint, Route
from moto_route.services import incidents
from moto_route.services.cache import TTLCache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path, autobahn_enabled=False, incident_feeds=[],
                    incident_corridor_m=500.0)


@pytest.fixture
def cache(tmp_path) -> TTLCache:
    return TTLCache(tmp_path, "incident-test")


@pytest.fixture
def route() -> Route:
    return Route(name="Test", lines=[[GeoPoint(lat=48.0 + i * 0.001, lon=11.0)
                                      for i in range(200)]])


# ------------------------------------------------------------- GeoJSON provider

def geojson(features):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": features})
    return handler


async def test_geojson_point_feature_is_read_lon_lat(route):
    """GeoJSON is [longitude, latitude] — the same trap KML sets."""
    handler = geojson([{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [11.0, 48.1]},
        "properties": {"title": "Lane closure", "type": "closure"},
    }])
    provider = incidents.GeoJsonFeedProvider("https://roads.example/feed.json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await provider.fetch(route, client)

    assert len(found) == 1
    assert found[0].lat == pytest.approx(48.1)
    assert found[0].lon == pytest.approx(11.0)
    assert found[0].category == "closure"


async def test_geojson_linestring_uses_its_midpoint(route):
    """The start of a 20 km roadworks stretch can be far off route while the
    works themselves are on it."""
    handler = geojson([{
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[11.0, 48.0], [11.0, 48.1], [11.0, 48.2]]},
        "properties": {"title": "Resurfacing"},
    }])
    provider = incidents.GeoJsonFeedProvider("https://roads.example/feed.json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await provider.fetch(route, client)

    assert found[0].lat == pytest.approx(48.1)


@pytest.mark.parametrize("raw,expected", [
    ("ROAD_CLOSED", "closure"),
    ("Baustelle", "roadworks"),
    ("roadworks", "roadworks"),
    ("WARNING", "warning"),
    ("something else", "incident"),
])
def test_category_normalisation_across_languages(raw, expected):
    assert incidents._normalise_category(raw) == expected


async def test_alternative_property_spellings_are_accepted(route):
    """Authorities disagree on field names; several spellings are tried."""
    handler = geojson([{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [11.0, 48.1]},
        "properties": {"headline": "Bridge works", "comment": "single lane",
                       "link": "https://example.org/x"},
    }])
    provider = incidents.GeoJsonFeedProvider("https://roads.example/feed.json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await provider.fetch(route, client)

    assert found[0].title == "Bridge works"
    assert found[0].detail == "single lane"
    assert found[0].url == "https://example.org/x"


async def test_a_non_geojson_response_is_rejected(route):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "nope"})

    provider = incidents.GeoJsonFeedProvider("https://roads.example/feed.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="FeatureCollection"):
            await provider.fetch(route, client)


async def test_malformed_features_are_skipped_not_fatal(route):
    handler = geojson([
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.1]},
         "properties": {"title": "Good"}},
        {"type": "Feature", "geometry": {}, "properties": {"title": "No geometry"}},
        "not even a dict",
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": ["x", "y"]},
         "properties": {}},
    ])
    provider = incidents.GeoJsonFeedProvider("https://roads.example/feed.json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await provider.fetch(route, client)

    assert [i.title for i in found] == ["Good"]


# ------------------------------------------------------------ Autobahn provider

def autobahn_handler(items_by_service, roads_payload=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":  # the Overpass road-detection query
            return httpx.Response(200, json=roads_payload or {"elements": []})
        service = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json={service: items_by_service.get(service, [])})
    return handler


async def test_autobahn_longitude_field_is_spelled_long(route, settings, cache):
    """The API uses "long", not "lon". Reading "lon" silently yields nothing."""
    settings.autobahn_enabled = True
    handler = autobahn_handler({"roadworks": [
        {"coordinate": {"lat": "48.1", "long": "11.0"}, "title": "A8 roadworks",
         "description": ["Right lane closed", "Until October"]},
    ]})
    provider = incidents.AutobahnProvider(settings, cache, roads=["A8"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await provider.fetch(route, client)

    works = [i for i in found if i.category == "roadworks"]
    assert len(works) == 1
    assert works[0].lat == pytest.approx(48.1)
    assert works[0].lon == pytest.approx(11.0)
    assert "Right lane closed Until October" in works[0].detail


async def test_autobahn_queries_all_three_services(route, settings, cache):
    settings.autobahn_enabled = True
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).rsplit("/", 1)[-1])
        return httpx.Response(200, json={})

    provider = incidents.AutobahnProvider(settings, cache, roads=["A8"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await provider.fetch(route, client)

    assert set(seen) == {"roadworks", "closure", "warning"}


async def test_autobahn_detects_roads_from_openstreetmap(route, settings, cache):
    """No manual configuration: the motorways are read off the route."""
    settings.autobahn_enabled = True
    roads_payload = {"elements": [
        {"type": "way", "tags": {"ref": "A 8"}},
        {"type": "way", "tags": {"ref": "A8"}},      # duplicate, differently spaced
        {"type": "way", "tags": {"ref": "E45"}},     # European route, not a German motorway
        {"type": "way", "tags": {"ref": "B27"}},     # federal road, not an Autobahn
    ]}
    queried = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=roads_payload)
        queried.append(str(request.url).split("/autobahn/")[-1].split("/")[0])
        return httpx.Response(200, json={})

    provider = incidents.AutobahnProvider(settings, cache)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await provider.fetch(route, client)

    assert set(queried) == {"A8"}, "A 8 and A8 are one road; E45 and B27 are not Autobahnen"


async def test_autobahn_fan_out_is_capped(route, settings, cache):
    settings.autobahn_enabled = True
    settings.max_autobahn_roads = 2
    queried = set()

    def handler(request: httpx.Request) -> httpx.Response:
        queried.add(str(request.url).split("/autobahn/")[-1].split("/")[0])
        return httpx.Response(200, json={})

    provider = incidents.AutobahnProvider(settings, cache, roads=["A1", "A2", "A3", "A4"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await provider.fetch(route, client)

    assert len(queried) == 2


async def test_one_failing_service_does_not_lose_the_others(route, settings, cache):
    settings.autobahn_enabled = True

    def handler(request: httpx.Request) -> httpx.Response:
        service = str(request.url).rsplit("/", 1)[-1]
        if service == "closure":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={service: [
            {"coordinate": {"lat": 48.1, "long": 11.0}, "title": f"{service} item"},
        ]})

    provider = incidents.AutobahnProvider(settings, cache, roads=["A8"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await provider.fetch(route, client)

    categories = {i.category for i in found}
    assert categories == {"roadworks", "warning"}


# ----------------------------------------------------------------- the service

async def test_no_configured_provider_says_so(route, settings, cache):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        result = await incidents.find_incidents(route, settings, client, cache)

    assert result["available"] is False
    assert "MOTO_INCIDENT_FEEDS" in result["reason"]


async def test_incidents_far_from_the_route_are_dropped(route, settings, cache):
    """A national feed covers a whole country."""
    settings.incident_feeds = ["https://roads.example/feed.json"]
    handler = geojson([
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.1]},
         "properties": {"title": "On my route"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
         "properties": {"title": "Berlin, 500 km away"}},
    ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await incidents.find_incidents(route, settings, client, cache)

    assert [i["title"] for i in result["incidents"]] == ["On my route"]


async def test_incidents_are_positioned_and_ordered_along_the_route(route, settings, cache):
    settings.incident_feeds = ["https://roads.example/feed.json"]
    handler = geojson([
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.15]},
         "properties": {"title": "Later"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.05]},
         "properties": {"title": "Earlier"}},
    ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await incidents.find_incidents(route, settings, client, cache)

    assert [i["title"] for i in result["incidents"]] == ["Earlier", "Later"]
    assert result["incidents"][0]["distance_along_route_m"] < \
           result["incidents"][1]["distance_along_route_m"]


async def test_one_dead_feed_does_not_sink_the_layer(route, settings, cache):
    settings.incident_feeds = ["https://good.example/f.json", "https://dead.example/f.json"]

    def handler(request: httpx.Request) -> httpx.Response:
        if "dead" in str(request.url):
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.1]},
             "properties": {"title": "Still here"}},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await incidents.find_incidents(route, settings, client, cache)

    assert result["available"] is True
    assert len(result["incidents"]) == 1
    assert any("dead.example" in name for name in result["failed_sources"])


async def test_offline_mode_makes_no_request(route, settings, cache):
    settings.offline = True
    settings.incident_feeds = ["https://roads.example/feed.json"]

    def handler(request):
        raise AssertionError("offline mode must not touch the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await incidents.find_incidents(route, settings, client, cache)

    assert result["available"] is False


# ------------------------------------------------- geographic coverage

def greek_route() -> Route:
    """Athens towards Thessaloniki. Greek motorways are numbered A1, A2, ...
    exactly as German ones are, which is the trap."""
    return Route(name="Greece", lines=[[GeoPoint(lat=37.98 + i * 0.01, lon=23.72 - i * 0.002)
                                        for i in range(200)]])


def german_route() -> Route:
    return Route(name="Bavaria", lines=[[GeoPoint(lat=48.13 + i * 0.005, lon=11.58 + i * 0.005)
                                         for i in range(100)]])


def test_autobahn_covers_germany_but_not_greece(settings, cache):
    provider = incidents.AutobahnProvider(settings, cache)

    assert provider.covers(german_route()) is True
    assert provider.covers(greek_route()) is False


def test_autobahn_covers_a_route_that_only_partly_enters_germany(settings, cache):
    """Munich to Salzburg is half Austrian; its German stretch still counts."""
    crossing = Route(name="DE-AT", lines=[[GeoPoint(lat=48.14, lon=11.58),
                                           GeoPoint(lat=47.80, lon=13.04)]])
    assert incidents.AutobahnProvider(settings, cache).covers(crossing) is True


async def test_autobahn_makes_no_request_for_a_route_outside_germany(settings, cache):
    """The bug this fixes: a Greek route spent an Overpass slot on a German
    feature, and Greek 'A1' would have been queried against the German API."""
    settings.autobahn_enabled = True

    def handler(request):
        raise AssertionError(f"no request should be made, got {request.url}")

    provider = incidents.AutobahnProvider(settings, cache)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await provider.fetch(greek_route(), client) == []


async def test_explicitly_configured_roads_still_win(settings, cache):
    """Pinning roads is a deliberate act; honour it wherever the route is."""
    settings.autobahn_enabled = True
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    provider = incidents.AutobahnProvider(settings, cache, roads=["A8"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await provider.fetch(greek_route(), client)

    assert seen, "explicitly configured roads must still be queried"


async def test_a_route_no_feed_covers_is_reported_clearly(settings, cache):
    settings.autobahn_enabled = True
    settings.incident_feeds = []

    def handler(request):
        raise AssertionError("nothing should be requested")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await incidents.find_incidents(greek_route(), settings, client, cache)

    assert result["available"] is False
    assert "covers this route" in result["reason"]
    assert "MOTO_INCIDENT_FEEDS" in result["reason"]


async def test_a_generic_geojson_feed_covers_everywhere(settings, cache):
    """A provider with no `covers` method must not be filtered out."""
    settings.incident_feeds = ["https://roads.example/feed.json"]
    settings.autobahn_enabled = False

    handler = geojson([{
        "type": "Feature",
        # Exactly on the route line, so the corridor filter is not what is
        # under test here.
        "geometry": {"type": "Point", "coordinates": [23.706, 38.05]},
        "properties": {"title": "Rockfall"},
    }])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await incidents.find_incidents(greek_route(), settings, client, cache)

    assert result["available"] is True
    assert [i["title"] for i in result["incidents"]] == ["Rockfall"]
