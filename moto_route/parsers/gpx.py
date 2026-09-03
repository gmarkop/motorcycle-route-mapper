"""GPX reader, including the Garmin flavours BaseCamp and Tread produce.

A GPX file can describe a ride in three different ways and a Garmin export
often uses two of them at once:

``<wpt>``
    Standalone points of interest. Fuel stops, viewpoints, your hotel.

``<trk>/<trkseg>/<trkpt>``
    A *track*: a dense breadcrumb line, either recorded by the GPS or generated
    by the planner. This is the line you actually want to draw.

``<rte>/<rtept>``
    A *route*: only the points you picked, to be re-calculated by the device.
    Garmin BaseCamp additionally hides the calculated road geometry inside each
    ``<rtept>`` as ``gpxx:rpt`` elements, so a route can be drawn accurately
    without re-routing it — as long as you know to look there.

When a file carries both a track and a route we draw the track (it is the
higher-fidelity line) and keep the route points as via points, because those
are the decisions the rider made.
"""

from __future__ import annotations

from typing import Iterable
from xml.etree import ElementTree as ET

from ..models import GeoPoint, Route, Waypoint
from .common import (
    RouteParseError,
    children,
    descendants,
    first_child,
    localname,
    parse_float,
    parse_time,
    parse_xml,
    text_of,
)


#: An unnamed route of at most this many points is read as the rider's own
#: stops. Google Maps allows about ten waypoints and other planners a few dozen;
#: anything longer is a driving polyline that a converter wrote out point by
#: point.
MAX_IMPLICIT_VIA_POINTS = 25

def parse_gpx(data: bytes) -> Route:
    root = parse_xml(data)
    if localname(root).lower() != "gpx":
        raise RouteParseError(
            f"Expected a <gpx> root element, found <{localname(root)}>."
        )

    route = Route(source_format="gpx")
    route.metadata["gpx_version"] = root.get("version", "")
    route.metadata["creator"] = root.get("creator", "")

    metadata_elem = first_child(root, "metadata")
    if metadata_elem is not None:
        route.name = text_of(metadata_elem, "name")

    # --- standalone waypoints -------------------------------------------------
    for wpt in children(root, "wpt"):
        point = _waypoint_from_element(wpt, kind="waypoint")
        if point is not None:
            route.waypoints.append(point)

    # --- tracks ---------------------------------------------------------------
    track_lines: list[list[GeoPoint]] = []
    track_names: list[str] = []
    for trk in children(root, "trk"):
        name = text_of(trk, "name")
        if name:
            track_names.append(name)
        for seg in children(trk, "trkseg"):
            line = _points_from(children(seg, "trkpt"))
            if len(line) >= 2:
                track_lines.append(line)

    # --- routes ---------------------------------------------------------------
    route_lines: list[list[GeoPoint]] = []
    route_names: list[str] = []
    for rte in children(root, "rte"):
        name = text_of(rte, "name")
        if name:
            route_names.append(name)
        line, via_points = _read_rte(rte)
        if len(line) >= 2:
            route_lines.append(line)
        route.waypoints.extend(via_points)

    if track_lines:
        route.lines = track_lines
        route.metadata["drawn_from"] = "track"
    else:
        route.lines = route_lines
        route.metadata["drawn_from"] = "route"

    route.metadata["track_count"] = len(track_lines)
    route.metadata["route_count"] = len(route_lines)

    if not route.name:
        candidates = track_names + route_names
        route.name = candidates[0] if candidates else "Unnamed route"

    if not route.lines and not route.waypoints:
        raise RouteParseError(
            "No track, route or waypoint found in this GPX file."
        )
    return route


def _points_from(elements: Iterable[ET.Element]) -> list[GeoPoint]:
    points: list[GeoPoint] = []
    for elem in elements:
        point = _geopoint_from_element(elem)
        if point is not None:
            points.append(point)
    return points


def _geopoint_from_element(elem: ET.Element) -> GeoPoint | None:
    lat = parse_float(elem.get("lat"))
    lon = parse_float(elem.get("lon"))
    if lat is None or lon is None or not _plausible(lat, lon):
        return None
    return GeoPoint(
        lat=lat,
        lon=lon,
        ele=parse_float(text_of(elem, "ele") or None),
        time=parse_time(text_of(elem, "time") or None),
    )


def _waypoint_from_element(elem: ET.Element, kind: str) -> Waypoint | None:
    lat = parse_float(elem.get("lat"))
    lon = parse_float(elem.get("lon"))
    if lat is None or lon is None or not _plausible(lat, lon):
        return None
    return Waypoint(
        lat=lat,
        lon=lon,
        name=text_of(elem, "name"),
        description=text_of(elem, "desc") or text_of(elem, "cmt"),
        symbol=text_of(elem, "sym"),
        kind=kind,
    )


def _read_rte(rte: ET.Element) -> tuple[list[GeoPoint], list[Waypoint]]:
    """Expand one ``<rte>`` into a drawable line plus its via/shaping points."""
    line: list[GeoPoint] = []
    via_points: list[Waypoint] = []

    rtepts = list(children(rte, "rtept"))

    # Does this file distinguish real stops from shaping points at all?
    #
    # Garmin marks them explicitly, or at least names the stops and leaves the
    # shaping points bare, so "unnamed" reliably means "shaping" there.
    distinguishes = any(
        _explicit_kind(rtept) is not None or text_of(rtept, "name")
        for rtept in rtepts
    )

    # Generic converters — the Google Maps to GPX tools — name nothing at all,
    # so for them the count is the only signal available. A rider picks a
    # handful of stops; a converter that dumps the whole driving polyline emits
    # hundreds of vertices. Treating the first case as shaping points hides the
    # rider's waypoints behind 3px grey dots; treating the second as via points
    # buries the map under hundreds of markers. Neither is a judgement call the
    # file lets us make properly, so the count decides.
    unnamed_are_stops = not distinguishes and len(rtepts) <= MAX_IMPLICIT_VIA_POINTS

    for rtept in rtepts:
        point = _geopoint_from_element(rtept)
        if point is None:
            continue
        line.append(point)

        kind = _classify_route_point(rtept, unnamed_are_stops=unnamed_are_stops)
        waypoint = _waypoint_from_element(rtept, kind=kind)
        if waypoint is not None:
            via_points.append(waypoint)

        # Garmin BaseCamp stores the calculated road geometry between this via
        # point and the next one as <gpxx:rpt> children. Without them a route
        # renders as straight lines between the stops.
        line.extend(_expanded_route_points(rtept))

    return line, via_points


def _expanded_route_points(rtept: ET.Element) -> list[GeoPoint]:
    extensions = first_child(rtept, "extensions")
    if extensions is None:
        return []
    points: list[GeoPoint] = []
    for rpt in descendants(extensions, "rpt"):
        lat = parse_float(rpt.get("lat"))
        lon = parse_float(rpt.get("lon"))
        if lat is not None and lon is not None and _plausible(lat, lon):
            points.append(GeoPoint(lat=lat, lon=lon))
    return points


def _explicit_kind(rtept: ET.Element) -> str | None:
    """The kind Garmin states outright inside ``<extensions>``, if it does."""
    extensions = first_child(rtept, "extensions")
    if extensions is None:
        return None
    for elem in extensions.iter():
        tag = localname(elem).lower()
        if tag == "shapingpoint":
            return "shaping"
        if tag == "viapoint":
            return "via"
    return None


def _classify_route_point(rtept: ET.Element, *, unnamed_are_stops: bool = False) -> str:
    """Tell a real stop from a point that only bends the line onto a nicer road.

    ``unnamed_are_stops`` is set when the surrounding route names nothing and is
    short enough that its points must be the rider's own choices rather than
    exported road geometry.
    """
    explicit = _explicit_kind(rtept)
    if explicit is not None:
        return explicit
    if text_of(rtept, "name"):
        return "via"
    return "via" if unnamed_are_stops else "shaping"


def _plausible(lat: float, lon: float) -> bool:
    """Reject coordinates outside the valid range, and the 'null island' 0/0
    that broken exporters emit for a point with no fix."""
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    return not (lat == 0.0 and lon == 0.0)
