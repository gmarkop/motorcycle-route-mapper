"""Export tests.

The strongest test here is a round trip: write the enriched GPX, then read it
back with this project's own parser. Anything malformed, mis-ordered or badly
escaped fails on the way back in.
"""

from xml.etree import ElementTree as ET

import re
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


def test_closures_say_they_are_closures(route, hazards):
    """A barrier arrived on the device as "No motor vehicles".

    Every other category names itself -- "Fuel:", "Hotel:", "Weather km 40:" --
    but hazards carried the raw OSM label alone, so the reason a road is shut
    was indistinguishable from a place you might want to visit.

    The OSM link left the description with it. In the panel it is clickable and
    worth having; on a phone in a tank bag it is a line of unreadable digits
    pushing the part that matters off a small screen.
    """
    reparsed = parse_route_bytes(export.build_gpx(route, hazards=hazards), "x.gpx")
    gate = next(w for w in reparsed.waypoints if "Closed gate" in (w.name or ""))

    # This fixture's label already opens with the word, so it is left alone --
    # "Closed: Closed gate" is how a prefix goes wrong.
    assert gate.name == "Closed gate"
    assert gate.symbol == export.SYMBOLS["hazard"]
    assert "seasonal: yes" in (gate.description or "")
    assert "openstreetmap.org" not in (gate.description or "")


def test_a_hazard_that_reads_like_a_place_is_named_as_a_closure():
    """"No motor vehicles" is a sign, not a destination.

    It was arriving on the device under that name alone, sitting in the same
    list as hotels and viewpoints with nothing to say it was the reason the
    road ahead is shut.
    """
    route = Route(name="t", source_format="gpx",
                  lines=[[GeoPoint(lat=40.0 + i * 0.01, lon=23.0) for i in range(6)]],
                  waypoints=[])
    payload = {"available": True, "hazards": [
        {"lat": 40.01, "lon": 23.0, "label": "No motor vehicles", "detail": "surface: asphalt",
         "severity": "closed", "distance_along_route_m": 1000, "osm_url": "https://osm.org/way/1"},
        {"lat": 40.02, "lon": 23.0, "label": "Weight limit 3.5t", "detail": "",
         "severity": "restricted", "distance_along_route_m": 2000, "osm_url": ""},
        {"lat": 40.03, "lon": 23.0, "label": "Resurfacing", "detail": "",
         "severity": "info", "distance_along_route_m": 3000, "osm_url": ""},
    ]}
    names = [w.name for w in
             parse_route_bytes(export.build_gpx(route, hazards=payload), "x.gpx").waypoints]

    assert "Closed: No motor vehicles" in names, names
    assert "Restricted: Weight limit 3.5t" in names, names
    assert "Roadworks: Resurfacing" in names, names


def test_a_hazard_label_that_already_says_closed_is_not_told_twice():
    """"Closed: Closed for the winter" is how a prefix goes wrong."""
    route = Route(name="t", source_format="gpx",
                  lines=[[GeoPoint(lat=40.0 + i * 0.01, lon=23.0) for i in range(4)]],
                  waypoints=[])
    payload = {"available": True, "hazards": [
        {"lat": 40.01, "lon": 23.0, "label": "Closed for the winter", "detail": "",
         "severity": "closed", "distance_along_route_m": 1000, "osm_url": ""},
    ]}
    names = [w.name for w in
             parse_route_bytes(export.build_gpx(route, hazards=payload), "x.gpx").waypoints]

    assert "Closed for the winter" in names, names
    assert "Closed: Closed for the winter" not in names, names


def test_planned_fuel_stops_are_numbered_and_the_rest_still_ship(route, pois):
    """This used to export the planned stops alone.

    The failure that happens on the road is a planned station being shut, and
    at that point the rider wants the next pump, not a tidier screen. So every
    station goes, and the numbering is what separates the plan from the rest:
    "Fuel stop 1" is the first stop the tank range depends on, not the first
    pump you ride past.
    """
    reparsed = parse_route_bytes(export.build_gpx(route, pois=pois), "x.gpx")
    names = [w.name for w in reparsed.waypoints]

    assert any(name.startswith("Fuel stop 1:") and "Aral" in name for name in names), names
    assert any(name == "Fuel: Shell" for name in names), names


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


# ------------------------------------------------- what reaches the device

def test_hotels_are_not_exported_as_viewpoints(route, pois):
    """They were, and with a generic flag.

    The prefix was `"Fuel stop" if fuel else "Viewpoint"`, so every category
    that was not fuel inherited the viewpoint label -- and SYMBOLS had no entry
    for accommodation, so it fell through to a blue flag. Once accommodation
    became a default layer, a hotel arrived on the device as
    "Viewpoint: Hotel Meteora". Nothing failed; it was just wrong, which is the
    kind of thing only a rider staring at a GPS notices.
    """
    payload = dict(pois)
    payload["pois"] = pois["pois"] + [
        {"lat": 48.2, "lon": 11.0, "category": "accommodation", "name": "Gasthof Alte Post",
         "detail": "", "distance_along_route_m": 21000},
        {"lat": 48.21, "lon": 11.0, "category": "motorcycle_parking", "name": "Marktplatz",
         "detail": "", "distance_along_route_m": 22000},
    ]
    reparsed = parse_route_bytes(export.build_gpx(route, pois=payload), "x.gpx")
    found = {w.name: w.symbol for w in reparsed.waypoints}

    assert found.get("Hotel: Gasthof Alte Post") == "Lodging", found
    assert found.get("Parking: Marktplatz") == "Parking Area", found
    assert not any("Viewpoint" in name and "Gasthof" in name for name in found), found


def test_a_category_label_is_not_repeated_when_the_name_already_says_it():
    """"Hotel: Hotel Meteora" reads like a bug on a handlebar screen."""
    route = Route(name="t", source_format="gpx",
                  lines=[[GeoPoint(lat=40.0 + i * 0.01, lon=23.0) for i in range(4)]],
                  waypoints=[])
    payload = {"available": True, "pois": [
        {"lat": 40.01, "lon": 23.0, "category": "accommodation", "name": "Hotel Meteora",
         "detail": "", "distance_along_route_m": 1000},
    ]}
    reparsed = parse_route_bytes(export.build_gpx(route, pois=payload), "x.gpx")

    assert "Hotel Meteora" in [w.name for w in reparsed.waypoints]
    assert "Hotel: Hotel Meteora" not in [w.name for w in reparsed.waypoints]


def test_demanding_stretches_become_waypoints_at_their_start():
    """The app cannot come on the ride; this is how the warning does.

    Placed at the start and named with length, direction and gradient, because
    a device shows the name long before anyone opens the description -- and
    "3.2 km down at 8%" changes gear choice before the first hairpin, which is
    the entire point of knowing.
    """
    route = Route(name="t", source_format="gpx",
                  lines=[[GeoPoint(lat=40.0 + i * 0.01, lon=23.0) for i in range(12)]],
                  waypoints=[])
    stretches = [{"from_m": 4500, "to_m": 7700, "length_m": 3200, "curviness": 210.0,
                  "gradient_pct": -8.4, "descending": True,
                  "from_lat": 40.045, "from_lon": 23.0}]

    reparsed = parse_route_bytes(export.build_gpx(route, demanding=stretches), "x.gpx")
    marks = [w for w in reparsed.waypoints if "Twisty" in (w.name or "")]

    assert len(marks) == 1, [w.name for w in reparsed.waypoints]
    assert "3.2 km" in marks[0].name and "down" in marks[0].name, marks[0].name
    assert marks[0].symbol == "Summit"
    assert marks[0].lat == pytest.approx(40.045)


def test_a_stretch_with_no_coordinates_is_skipped_rather_than_placed_at_null_island():
    """A cached payload from before the coordinates were added has neither.

    Writing it anyway would put a waypoint at 0,0 — in the Gulf of Guinea — on
    a device the rider is trusting.

    Asserted against the raw XML, not a re-import: `parsers/gpx.py` rejects
    null island in `_plausible`, so reading the file back would hide the bug.
    A first version of this test did exactly that and passed with the guard
    deleted — the parser was cleaning up after the exporter, and only another
    device would ever have seen it.
    """
    route = Route(name="t", source_format="gpx",
                  lines=[[GeoPoint(lat=40.0 + i * 0.01, lon=23.0) for i in range(4)]],
                  waypoints=[])
    stretches = [{"from_m": 1000, "length_m": 2000, "curviness": 200.0,
                  "gradient_pct": 9.0, "descending": False}]

    root = ET.fromstring(export.build_gpx(route, demanding=stretches))
    points = root.findall(f"{{{export.GPX_NAMESPACE}}}wpt")

    assert points == [], [p.attrib for p in points]


def test_fuel_stops_are_numbered_in_riding_order_not_payload_order():
    """The number is the promise: "Fuel stop 2" is the plan's second stop."""
    route = Route(name="t", source_format="gpx",
                  lines=[[GeoPoint(lat=40.0 + i * 0.01, lon=23.0) for i in range(12)]],
                  waypoints=[])
    payload = {"available": True, "pois": [
        {"lat": 40.07, "lon": 23.0, "category": "fuel", "name": "Late", "detail": "",
         "recommended": True, "distance_along_route_m": 7000},
        {"lat": 40.01, "lon": 23.0, "category": "fuel", "name": "Early", "detail": "",
         "recommended": True, "distance_along_route_m": 1000},
    ]}
    names = [w.name for w in
             parse_route_bytes(export.build_gpx(route, pois=payload), "x.gpx").waypoints]

    assert "Fuel stop 1: Early" in names, names
    assert "Fuel stop 2: Late" in names, names


def test_how_far_off_the_route_a_stop_sits_reaches_the_device(route, pois):
    """The device cannot show the panel's off-route line, and a station a
    kilometre up a side road is a different decision at 19:00 in the rain."""
    payload = dict(pois)
    payload["pois"] = [
        {"lat": 48.02, "lon": 11.0, "category": "fuel", "name": "Far", "detail": "24h",
         "recommended": False, "distance_along_route_m": 2200, "distance_off_route_m": 1450},
        {"lat": 48.03, "lon": 11.0, "category": "fuel", "name": "Near", "detail": "",
         "recommended": False, "distance_along_route_m": 3300, "distance_off_route_m": 20},
    ]
    found = {w.name: (w.description or "")
             for w in parse_route_bytes(export.build_gpx(route, pois=payload), "x.gpx").waypoints}

    assert "1.4 km off route" in found.get("Fuel: Far", ""), found
    assert "off route" not in found.get("Fuel: Near", ""), found


def test_a_stops_only_file_carries_no_track_at_all(route, pois):
    """This is the whole point of it, not a size optimisation.

    Given both waypoints and a track, several navigation apps -- Scenic among
    them -- take the waypoints for the route's via points, because that is the
    shape a Garmin route file has, and show them numbered "1, 2, 3" in place
    of their names. A single leftover <trk> is enough to trigger that, so the
    absence is the feature.
    """
    body = export.build_gpx(route, pois=pois, include_track=False)
    root = ET.fromstring(body)

    assert root.findall(f"{{{export.GPX_NAMESPACE}}}trk") == []
    assert root.findall(f"{{{export.GPX_NAMESPACE}}}wpt") != []
    # And the names are still the names, which is what was lost on the device.
    names = [w.name for w in parse_route_bytes(body, "x.gpx").waypoints]
    assert any("Aral" in name for name in names), names


def test_a_route_only_file_carries_no_waypoints(route, pois):
    """The other half of the pair: import this as the route, stops separately."""
    root = ET.fromstring(export.build_gpx(route, pois=pois, include_waypoints=False))

    assert root.findall(f"{{{export.GPX_NAMESPACE}}}wpt") == []
    assert root.findall(f"{{{export.GPX_NAMESPACE}}}trk") != []


def test_the_default_export_is_unchanged_by_the_split(route, weather, hazards, pois):
    """Splitting must not quietly change what the existing button produces."""
    both = ET.fromstring(export.build_gpx(route, weather, hazards, pois))

    assert both.findall(f"{{{export.GPX_NAMESPACE}}}wpt") != []
    assert both.findall(f"{{{export.GPX_NAMESPACE}}}trk") != []


def test_the_download_names_say_which_file_you_are_holding(route):
    """Two GPX files for one ride, in a phone's downloads folder, a week later."""
    assert export.suggested_filename(route).endswith("_enriched.gpx")
    assert export.suggested_filename(route, "stops").endswith("_stops.gpx")
    assert export.suggested_filename(route) != export.suggested_filename(route, "stops")


# ------------------------------------------------ choosing what goes on the device

def _rich_pois() -> dict:
    return {"available": True, "pois": [
        {"lat": 48.02, "lon": 11.0, "category": "fuel", "name": "Planned", "detail": "",
         "recommended": True, "distance_along_route_m": 2200},
        {"lat": 48.03, "lon": 11.0, "category": "fuel", "name": "Other", "detail": "",
         "recommended": False, "distance_along_route_m": 3300},
        {"lat": 48.04, "lon": 11.0, "category": "cafe", "name": "Alpin", "detail": "",
         "distance_along_route_m": 4400},
        {"lat": 48.12, "lon": 11.0, "category": "accommodation", "name": "Post", "detail": "",
         "distance_along_route_m": 12000},
    ]}


def test_the_export_writes_only_the_categories_asked_for(route):
    """A 450 km route carries a hundred pumps; the device screen is small."""
    names = lambda inc: [w.name for w in parse_route_bytes(  # noqa: E731
        export.build_gpx(route, pois=_rich_pois(), include=inc), "x.gpx").waypoints]

    assert names({"accommodation"}) == ["Hotel: Post"]
    assert names({"fuel"}) == ["Fuel stop 1: Planned"]
    assert names(set()) == [], "ticking nothing means nothing, not everything"


def test_wanting_the_plan_is_not_wanting_every_pump(route):
    """The whole reason fuel is split in two.

    "fuel" is the handful of stops the tank range depends on. "fuel_all" is the
    hundred others, which are insurance when a planned stop is shut and clutter
    otherwise — a separate decision, so a separate box.
    """
    def names(inc):
        return [w.name for w in parse_route_bytes(
            export.build_gpx(route, pois=_rich_pois(), include=inc), "x.gpx").waypoints]

    assert names({"fuel"}) == ["Fuel stop 1: Planned"]
    assert names({"fuel", "fuel_all"}) == ["Fuel stop 1: Planned", "Fuel: Other"]
    # Asking only for the others still gives the plan: leaving the stops the
    # range depends on out of a fuel export would be a trap, not a filter.
    assert "Fuel stop 1: Planned" in names({"fuel", "fuel_all"})


def test_cafes_are_exportable_but_not_by_default(route):
    """They were refused outright before. Now they are a box nobody ticked.

    Left out by default because every cafe within 300 m of a 400 km ride is not
    a useful device waypoint, and a GPX has nowhere to put the opening hours
    that would make one worth choosing.
    """
    default = [w.name for w in parse_route_bytes(
        export.build_gpx(route, pois=_rich_pois()), "x.gpx").waypoints]
    asked = [w.name for w in parse_route_bytes(
        export.build_gpx(route, pois=_rich_pois(), include={"cafe"}), "x.gpx").waypoints]

    assert not any("Alpin" in name for name in default), default
    assert asked == ["Cafe: Alpin"], asked
    assert "cafe" in export.EXPORTABLE
    assert "cafe" not in export.DEFAULT_INCLUDE


def test_an_export_asking_for_nothing_in_particular_is_unchanged(route, weather, hazards, pois):
    """`include=None` must keep meaning what it has always meant."""
    before = export.build_gpx(route, weather, hazards, pois)
    same = export.build_gpx(route, weather, hazards, pois, include=export.DEFAULT_INCLUDE)

    strip = lambda body: re.sub(rb"<time>.*?</time>|Enriched [^<]*", b"", body)  # noqa: E731
    assert strip(before) == strip(same)
