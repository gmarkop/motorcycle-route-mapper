"""Export tests.

The strongest test here is a round trip: write the enriched GPX, then read it
back with this project's own parser. Anything malformed, mis-ordered or badly
escaped fails on the way back in.
"""

from xml.etree import ElementTree as ET

import pytest

from moto_route import export
from moto_route.models import GeoPoint, Route, Waypoint
from moto_route.parsers import parse_route_bytes


@pytest.fixture
def route() -> Route:
    points = [GeoPoint(lat=48.0 + i * 0.01, lon=11.0, ele=500.0 + i) for i in range(20)]
    route = Route(name="Alpine Test", lines=[points])
    route.waypoints = [
        Waypoint(lat=48.0, lon=11.0, name="Start", kind="via"),
        Waypoint(lat=48.1, lon=11.0, name="", kind="shaping"),
        Waypoint(lat=48.19, lon=11.0, name="Finish", kind="via"),
    ]
    route.annotate_waypoint_distances()
    return route


@pytest.fixture
def weather() -> dict:
    return {"available": True, "summary": {"verdict": "Marginal. Expect a soaking."},
            "points": [
                {"lat": 48.05, "lon": 11.0, "distance_m": 5000, "description": "Clear sky",
                 "rideability": 100, "warnings": []},
                {"lat": 48.15, "lon": 11.0, "distance_m": 16000, "description": "Thunderstorm",
                 "rideability": 20, "warnings": ["Thunderstorms forecast", "Strong gusts"]},
            ]}


@pytest.fixture
def hazards() -> dict:
    return {"available": True, "hazards": [
        {"lat": 48.08, "lon": 11.0, "label": "Closed gate", "detail": "seasonal: yes",
         "severity": "closed", "distance_along_route_m": 8900,
         "osm_url": "https://www.openstreetmap.org/node/1"},
    ]}


@pytest.fixture
def pois() -> dict:
    return {"available": True, "fuel_plan": {"gaps": [{"length_km": 140.0}]}, "pois": [
        {"lat": 48.02, "lon": 11.0, "category": "fuel", "name": "Aral", "detail": "24/7",
         "recommended": True, "distance_along_route_m": 2200},
        {"lat": 48.03, "lon": 11.0, "category": "fuel", "name": "Shell", "detail": "",
         "recommended": False, "distance_along_route_m": 3300},
        {"lat": 48.04, "lon": 11.0, "category": "cafe", "name": "Cafe Alpin", "detail": "",
         "distance_along_route_m": 4400},
        {"lat": 48.12, "lon": 11.0, "category": "viewpoint", "name": "Panorama",
         "detail": "ele: 2100", "distance_along_route_m": 13300},
    ]}


# --------------------------------------------------------------- round tripping

def test_exported_gpx_parses_back_in(route, weather, hazards, pois):
    data = export.build_gpx(route, weather, hazards, pois)
    reparsed = parse_route_bytes(data, "enriched.gpx")

    assert reparsed.name == "Alpine Test"
    assert reparsed.point_count == 20
    assert reparsed.lines[0][0].ele == pytest.approx(500.0)


def test_schema_element_order_is_metadata_waypoints_track(route):
    """GPX 1.1 is a sequence type; wpt after trk is invalid."""
    root = ET.fromstring(export.build_gpx(route))
    tags = [child.tag.rsplit("}", 1)[-1] for child in root]

    assert tags[0] == "metadata"
    assert tags.count("trk") == 1
    assert tags.index("trk") == len(tags) - 1, "every wpt must precede the trk"


def test_declares_gpx_11_and_a_creator(route):
    root = ET.fromstring(export.build_gpx(route))
    assert root.get("version") == "1.1"
    assert "moto-route-mapper" in root.get("creator", "")


# ------------------------------------------------------------- what gets marked

def test_weather_warnings_become_waypoints(route, weather):
    reparsed = parse_route_bytes(export.build_gpx(route, weather=weather), "x.gpx")
    names = [w.name for w in reparsed.waypoints]

    assert any("Thunderstorm" in name for name in names)
    assert not any("Clear sky" in name for name in names), \
        "a forecast with no warning is noise on a device screen"


def test_weather_waypoint_carries_the_reasons_and_the_score(route, weather):
    reparsed = parse_route_bytes(export.build_gpx(route, weather=weather), "x.gpx")
    storm = next(w for w in reparsed.waypoints if "Thunderstorm" in w.name)

    assert "Thunderstorms forecast" in storm.description
    assert "rideability 20/100" in storm.description


def test_closures_become_waypoints(route, hazards):
    reparsed = parse_route_bytes(export.build_gpx(route, hazards=hazards), "x.gpx")
    gate = next(w for w in reparsed.waypoints if w.name == "Closed gate")

    assert gate.symbol == export.SYMBOLS["hazard"]
    assert "openstreetmap.org" in gate.description


def test_only_recommended_fuel_stops_are_exported(route, pois):
    reparsed = parse_route_bytes(export.build_gpx(route, pois=pois), "x.gpx")
    names = [w.name for w in reparsed.waypoints]

    assert any("Aral" in name for name in names)
    assert not any("Shell" in name for name in names), \
        "only pumps the plan depends on belong on the device"


def test_cafes_are_left_out_but_viewpoints_are_kept(route, pois):
    reparsed = parse_route_bytes(export.build_gpx(route, pois=pois), "x.gpx")
    names = " ".join(w.name for w in reparsed.waypoints)

    assert "Cafe Alpin" not in names
    assert "Panorama" in names


def test_garmin_symbols_are_used(route, pois):
    reparsed = parse_route_bytes(export.build_gpx(route, pois=pois), "x.gpx")
    symbols = {w.symbol for w in reparsed.waypoints}

    assert "Gas Station" in symbols
    assert "Scenic Area" in symbols


def test_shaping_points_are_excluded_by_default(route):
    reparsed = parse_route_bytes(export.build_gpx(route), "x.gpx")
    assert all(w.name != "" for w in reparsed.waypoints)

    with_shaping = parse_route_bytes(
        export.build_gpx(route, include_shaping_points=True), "x.gpx")
    assert len(with_shaping.waypoints) > len(reparsed.waypoints)


def test_waypoints_are_written_in_route_order(route, weather, hazards, pois):
    root = ET.fromstring(export.build_gpx(route, weather, hazards, pois))
    lats = [float(w.get("lat")) for w in root
            if w.tag.rsplit("}", 1)[-1] == "wpt"]

    assert lats == sorted(lats), "the route runs south to north, so should the file"


# ------------------------------------------------------------ robustness

def test_missing_or_failed_layers_are_tolerated(route):
    failed = {"available": False, "reason": "Overpass down", "hazards": []}
    data = export.build_gpx(route, weather=None, hazards=failed, pois=None)

    reparsed = parse_route_bytes(data, "x.gpx")
    assert reparsed.point_count == 20


def test_xml_special_characters_are_escaped(route):
    """A waypoint name is user data and can contain anything."""
    route.waypoints = [Waypoint(lat=48.0, lon=11.0, kind="via",
                                name='Café & Bar <script>"x"',
                                description="a > b & c")]
    data = export.build_gpx(route)

    reparsed = parse_route_bytes(data, "x.gpx")
    assert reparsed.waypoints[0].name == 'Café & Bar <script>"x"'
    assert b"<script>" not in data, "the tag must be escaped, not embedded raw"


def test_metadata_description_summarises_the_enrichment(route, weather, pois):
    root = ET.fromstring(export.build_gpx(route, weather=weather, pois=pois))
    desc = root.find("{http://www.topografix.com/GPX/1/1}metadata/"
                     "{http://www.topografix.com/GPX/1/1}desc").text

    assert "km" in desc
    assert "Marginal" in desc
    assert "140" in desc, "the longest dry stretch belongs in the summary"


def test_segments_are_preserved_as_separate_tracks():
    route = Route(name="Two legs", lines=[
        [GeoPoint(lat=48.0, lon=11.0), GeoPoint(lat=48.1, lon=11.0)],
        [GeoPoint(lat=48.5, lon=11.5), GeoPoint(lat=48.6, lon=11.5)],
    ])
    reparsed = parse_route_bytes(export.build_gpx(route), "x.gpx")
    assert len(reparsed.lines) == 2


def test_suggested_filename_is_filesystem_safe():
    route = Route(name="Alps: Bolzano / Cortina *2026*")
    name = export.suggested_filename(route)

    assert name.endswith("_enriched.gpx")
    assert not set(name) & set('/:*?"<>|')


def test_suggested_filename_survives_an_unnamed_route():
    assert export.suggested_filename(Route(name="")).endswith("_enriched.gpx")
