"""Parser tests, driven by fixture files that mirror what real exporters emit."""

import io
import zipfile

import pytest

from moto_route.parsers import RouteParseError, detect_format, parse_route_bytes


# ------------------------------------------------------------ format detection

def test_detect_format_prefers_content_over_extension(read_fixture):
    gpx = read_fixture("garmin_route.gpx")
    # Even mislabelled as .kml, the content wins.
    assert detect_format(gpx, "route.kml") == "gpx"


def test_detect_format_recognises_kml_and_kmz(read_fixture):
    assert detect_format(read_fixture("google_earth.kml"), "x.kml") == "kml"
    assert detect_format(b"PK\x03\x04rest-of-a-zip", "x.kmz") == "kmz"


def test_detect_format_falls_back_to_the_extension():
    assert detect_format(b"<?xml version='1.0'?><something/>", "ride.gpx") == "gpx"


def test_detect_format_rejects_unknown_content():
    with pytest.raises(RouteParseError):
        detect_format(b"just some text", "notes.txt")


# ------------------------------------------------------------------------ GPX

def test_garmin_route_expands_shaping_geometry(read_fixture):
    route = parse_route_bytes(read_fixture("garmin_route.gpx"), "garmin_route.gpx")

    assert route.name == "Stelvio Loop"
    assert route.source_format == "gpx"
    assert route.metadata["drawn_from"] == "route"
    # 3 rtept plus the 4 gpxx:rpt points hidden in their extensions.
    assert route.point_count == 7


def test_garmin_route_distinguishes_via_from_shaping_points(read_fixture):
    route = parse_route_bytes(read_fixture("garmin_route.gpx"), "garmin_route.gpx")
    kinds = [w.kind for w in route.waypoints]

    assert kinds.count("via") == 2         # the two named trp:ViaPoint entries
    assert kinds.count("shaping") == 1     # the trp:ShapingPoint
    assert kinds.count("waypoint") == 1    # the standalone <wpt>


def test_standalone_waypoint_keeps_its_description_and_symbol(read_fixture):
    route = parse_route_bytes(read_fixture("garmin_route.gpx"), "garmin_route.gpx")
    summit = next(w for w in route.waypoints if w.kind == "waypoint")

    assert summit.name == "Passo dello Stelvio"
    assert summit.description == "Summit cafe, cash only"
    assert summit.symbol == "Summit"


def test_waypoints_are_positioned_along_the_route(read_fixture):
    route = parse_route_bytes(read_fixture("garmin_route.gpx"), "garmin_route.gpx")
    start = next(w for w in route.waypoints if w.name == "Prad am Stilfserjoch")
    summit = next(w for w in route.waypoints if w.kind == "waypoint")

    assert start.distance_m == pytest.approx(0.0, abs=50)
    assert summit.distance_m > start.distance_m


def test_track_segments_stay_separate(read_fixture):
    route = parse_route_bytes(read_fixture("track.gpx"), "track.gpx")

    assert route.metadata["drawn_from"] == "track"
    assert len(route.lines) == 2, "a gap in the recording must not become a straight line"
    assert [len(line) for line in route.lines] == [4, 2]


def test_track_distance_excludes_the_gap_between_segments(read_fixture):
    route = parse_route_bytes(read_fixture("track.gpx"), "track.gpx")
    joined = sum(
        1 for _ in route.iter_points()
    )
    assert joined == 6
    # The 3 km jump between segments must not be counted as ridden.
    assert route.distance_m < 6000


def test_track_reads_elevation_and_time(read_fixture):
    route = parse_route_bytes(read_fixture("track.gpx"), "track.gpx")
    first = route.lines[0][0]

    assert first.ele == pytest.approx(500.0)
    assert first.time is not None
    assert first.time.year == 2026


def test_elevation_gain_ignores_small_jitter(read_fixture):
    route = parse_route_bytes(read_fixture("track.gpx"), "track.gpx")
    ascent, descent = route.elevation_gain_m()

    assert ascent == pytest.approx(165, abs=1)   # 500->560->620, then 545->590
    assert descent == pytest.approx(80, abs=1)   # 620->540


def test_gpx_with_no_geometry_is_rejected():
    empty = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"></gpx>"""
    with pytest.raises(RouteParseError, match="No track, route or waypoint"):
        parse_route_bytes(empty, "empty.gpx")


def test_gpx_10_namespace_is_read_the_same_way():
    gpx10 = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/0" version="1.0">
      <trk><trkseg>
        <trkpt lat="48.0" lon="11.0"/><trkpt lat="48.1" lon="11.1"/>
      </trkseg></trk>
    </gpx>"""
    route = parse_route_bytes(gpx10, "old.gpx")
    assert route.point_count == 2


def test_implausible_coordinates_are_dropped():
    gpx = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
      <trk><trkseg>
        <trkpt lat="48.0" lon="11.0"/>
        <trkpt lat="0.0" lon="0.0"/>
        <trkpt lat="99.0" lon="11.0"/>
        <trkpt lat="48.1" lon="11.1"/>
      </trkseg></trk>
    </gpx>"""
    route = parse_route_bytes(gpx, "noisy.gpx")
    assert route.point_count == 2, "null island and out-of-range points must go"


# ------------------------------------------------------------------------ KML

def test_kml_reads_longitude_first(read_fixture):
    route = parse_route_bytes(read_fixture("google_earth.kml"), "google_earth.kml")
    first = route.lines[0][0]

    # The file says "6.4000,45.0500" — longitude then latitude.
    assert first.lat == pytest.approx(45.05)
    assert first.lon == pytest.approx(6.40)


def test_kml_point_placemark_becomes_a_waypoint(read_fixture):
    route = parse_route_bytes(read_fixture("google_earth.kml"), "google_earth.kml")

    assert len(route.waypoints) == 1
    galibier = route.waypoints[0]
    assert galibier.name == "Col du Galibier"
    assert "closed in winter" in galibier.description
    assert "<b>" not in galibier.description, "HTML must be stripped for display"


def test_kml_reads_altitude_from_the_third_coordinate(read_fixture):
    route = parse_route_bytes(read_fixture("google_earth.kml"), "google_earth.kml")
    assert route.lines[0][2].ele == pytest.approx(2642.0)


def test_gx_track_pairs_timestamps_with_coordinates():
    kml = b"""<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
      <Document><Placemark><gx:Track>
        <when>2026-06-01T07:00:00Z</when>
        <when>2026-06-01T07:01:00Z</when>
        <gx:coord>11.0 48.0 500</gx:coord>
        <gx:coord>11.1 48.1 520</gx:coord>
      </gx:Track></Placemark></Document>
    </kml>"""
    route = parse_route_bytes(kml, "track.kml")
    points = route.lines[0]

    assert len(points) == 2
    # gx:coord is space separated and still longitude first.
    assert points[0].lat == pytest.approx(48.0)
    assert points[0].ele == pytest.approx(500.0)
    assert points[1].time is not None and points[1].time.minute == 1


def test_multigeometry_produces_several_lines():
    kml = b"""<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><MultiGeometry>
      <LineString><coordinates>11.0,48.0 11.1,48.1</coordinates></LineString>
      <LineString><coordinates>11.5,48.5 11.6,48.6</coordinates></LineString>
    </MultiGeometry></Placemark></Document></kml>"""
    route = parse_route_bytes(kml, "multi.kml")
    assert len(route.lines) == 2


def test_malformed_coordinate_tuples_are_skipped():
    kml = b"""<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><LineString>
      <coordinates>11.0,48.0 broken 11.1,48.1 12.0</coordinates>
    </LineString></Placemark></Document></kml>"""
    route = parse_route_bytes(kml, "messy.kml")
    assert route.point_count == 2


# ------------------------------------------------------------------------ KMZ

def test_kmz_archive_is_unpacked(read_fixture):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.kml", read_fixture("google_earth.kml"))

    route = parse_route_bytes(buffer.getvalue(), "ride.kmz")
    assert route.source_format == "kmz"
    assert route.metadata["kmz_entry"] == "doc.kml"
    assert route.name == "Route des Grandes Alpes"


def test_kmz_without_a_kml_document_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "not a route")

    with pytest.raises(RouteParseError, match="no .kml document"):
        parse_route_bytes(buffer.getvalue(), "ride.kmz")


# --------------------------------------------------------------------- safety

def test_entity_expansion_attack_is_refused():
    """The 'billion laughs' bomb: nested entities that expand exponentially."""
    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE gpx [
      <!ENTITY a "aaaaaaaaaa">
      <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
      <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
    ]>
    <gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>&c;</name></trk></gpx>"""
    with pytest.raises(RouteParseError, match="DTD or XML entities"):
        parse_route_bytes(bomb, "bomb.gpx")


def test_empty_file_is_rejected():
    with pytest.raises(RouteParseError, match="empty"):
        parse_route_bytes(b"", "nothing.gpx")


def test_invalid_xml_reports_a_readable_error():
    with pytest.raises(RouteParseError, match="not valid XML"):
        parse_route_bytes(b"<gpx><trk></gpx>", "truncated.gpx")


def test_wrong_root_element_is_reported():
    with pytest.raises(RouteParseError, match="Expected a <gpx> root"):
        parse_route_bytes(b"<?xml version='1.0'?><gpx-ish/>", "weird.gpx")


# ------------------------------------- route files with no naming convention

def test_a_converter_route_with_no_named_points_stays_visible():
    """Google Maps to GPX tools name nothing.

    Reading "unnamed" as "shaping point" there marked every point on the route
    as scenery, and the frontend draws shaping points as 3px grey dots — so the
    rider's waypoints simply vanished.
    """
    gpx = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="online converter">
      <rte><name>Athens to Delphi</name>
        <rtept lat="37.9838" lon="23.7275"/>
        <rtept lat="38.1800" lon="23.1000"/>
        <rtept lat="38.4824" lon="22.5010"/>
      </rte>
    </gpx>"""
    route = parse_route_bytes(gpx, "converted.gpx")

    assert [w.kind for w in route.waypoints] == ["via", "via", "via"]


def test_a_long_unnamed_route_is_read_as_geometry_not_stops():
    """The other half of the same judgement.

    A converter that writes the whole driving polyline out as route points
    produces hundreds of them. Calling those via points buries the map under
    hundreds of markers, which is no more useful than hiding them.
    """
    points = "".join(
        f'<rtept lat="{38.0 + i * 0.01:.4f}" lon="{23.0 + i * 0.005:.4f}"/>'
        for i in range(120)
    )
    gpx = ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" '
           f'version="1.1"><rte>{points}</rte></gpx>').encode()

    route = parse_route_bytes(gpx, "polyline.gpx")

    assert {w.kind for w in route.waypoints} == {"shaping"}


def test_the_via_point_threshold_is_where_it_claims_to_be():
    from moto_route.parsers.gpx import MAX_IMPLICIT_VIA_POINTS

    def route_of(count: int):
        points = "".join(
            f'<rtept lat="{38.0 + i * 0.01:.4f}" lon="23.0"/>' for i in range(count)
        )
        gpx = ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" '
               f'version="1.1"><rte>{points}</rte></gpx>').encode()
        return parse_route_bytes(gpx, "x.gpx")

    at_limit = route_of(MAX_IMPLICIT_VIA_POINTS)
    over_limit = route_of(MAX_IMPLICIT_VIA_POINTS + 1)

    assert {w.kind for w in at_limit.waypoints} == {"via"}
    assert {w.kind for w in over_limit.waypoints} == {"shaping"}


def test_a_file_that_does_distinguish_is_still_respected(read_fixture):
    """Garmin names its stops and leaves shaping points bare; keep reading that."""
    route = parse_route_bytes(read_fixture("garmin_route.gpx"), "garmin_route.gpx")
    kinds = [w.kind for w in route.waypoints]

    assert "shaping" in kinds, "an unnamed point beside named ones is still shaping"
    assert "via" in kinds


def test_one_named_point_is_enough_to_imply_the_convention():
    """If the author named anything, the unnamed ones are shaping points."""
    gpx = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
      <rte>
        <rtept lat="48.0" lon="11.0"><name>Coffee</name></rtept>
        <rtept lat="48.1" lon="11.1"/>
        <rtept lat="48.2" lon="11.2"/>
      </rte>
    </gpx>"""
    route = parse_route_bytes(gpx, "mixed.gpx")

    assert [w.kind for w in route.waypoints] == ["via", "shaping", "shaping"]
