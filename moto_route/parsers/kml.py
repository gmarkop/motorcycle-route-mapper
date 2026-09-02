"""KML / KMZ reader for Google Earth and My Maps exports.

The trap in KML is coordinate order. GPX writes ``lat="48.1" lon="11.6"`` as
attributes; KML writes text content as ``longitude,latitude[,altitude]`` —
**longitude first**. Swapping them silently puts a Bavarian ride in Somalia, so
every coordinate goes through one parser here.

Three geometries carry a ride:

``<LineString><coordinates>``
    The normal case: one whitespace-separated list of ``lon,lat,ele`` triples.

``<MultiGeometry>``
    Several LineStrings in one placemark, e.g. a route split by a ferry.

``<gx:Track>``
    Google's time-aware track: alternating ``<when>`` timestamps and
    ``<gx:coord>`` elements, where the coordinate is *space*-separated rather
    than comma-separated. Same data, different punctuation.

A ``.kmz`` is just a zip archive with the KML inside it, usually ``doc.kml``.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
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


def parse_kml(data: bytes) -> Route:
    root = parse_xml(data)
    if localname(root).lower() != "kml":
        # Some exporters hand out a bare <Document> without the <kml> wrapper.
        if localname(root).lower() not in {"document", "folder"}:
            raise RouteParseError(
                f"Expected a <kml> root element, found <{localname(root)}>."
            )

    route = Route(source_format="kml")

    document = first_child(root, "Document") or first_child(root, "Folder") or root
    route.name = text_of(document, "name")

    for placemark in descendants(root, "Placemark"):
        _read_placemark(placemark, route)

    if not route.name:
        route.name = "Unnamed route"

    if not route.lines and not route.waypoints:
        raise RouteParseError(
            "No line or point geometry found in this KML file."
        )
    route.metadata["line_count"] = len(route.lines)
    return route


def parse_kmz(data: bytes) -> Route:
    """Unpack a KMZ archive and parse the KML document inside it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise RouteParseError(f"File is not a readable KMZ archive: {exc}") from exc

    with archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise RouteParseError("This KMZ archive contains no .kml document.")
        # "doc.kml" is the conventional entry point; fall back to the first one.
        preferred = next((n for n in names if n.lower().endswith("doc.kml")), names[0])
        info = archive.getinfo(preferred)
        # Guard against a zip bomb: refuse an entry that expands absurdly.
        if info.file_size > 50 * 1024 * 1024:
            raise RouteParseError("The KML inside this archive is too large to parse.")
        payload = archive.read(preferred)

    route = parse_kml(payload)
    route.source_format = "kmz"
    route.metadata["kmz_entry"] = preferred
    return route


def _read_placemark(placemark: ET.Element, route: Route) -> None:
    name = text_of(placemark, "name")
    description = _clean_description(text_of(placemark, "description"))

    # A placemark holds either point(s) or line(s); handle both, since a
    # MultiGeometry can legitimately mix them.
    for line_string in descendants(placemark, "LineString"):
        points = _coordinates_to_points(text_of(line_string, "coordinates"))
        if len(points) >= 2:
            route.lines.append(points)

    for track in descendants(placemark, "Track"):
        points = _read_gx_track(track)
        if len(points) >= 2:
            route.lines.append(points)

    for point_elem in descendants(placemark, "Point"):
        points = _coordinates_to_points(text_of(point_elem, "coordinates"))
        if points:
            first = points[0]
            route.waypoints.append(
                Waypoint(
                    lat=first.lat,
                    lon=first.lon,
                    name=name,
                    description=description,
                    kind="waypoint",
                )
            )


def _read_gx_track(track: ET.Element) -> list[GeoPoint]:
    """Zip ``<when>`` timestamps together with ``<gx:coord>`` positions."""
    times = [parse_time(w.text) for w in children(track, "when")]
    coords = [c.text or "" for c in children(track, "coord")]

    points: list[GeoPoint] = []
    for index, raw in enumerate(coords):
        parts = raw.split()
        if len(parts) < 2:
            continue
        lon, lat = parse_float(parts[0]), parse_float(parts[1])
        ele = parse_float(parts[2]) if len(parts) > 2 else None
        if lat is None or lon is None or not _plausible(lat, lon):
            continue
        when = times[index] if index < len(times) else None
        points.append(GeoPoint(lat=lat, lon=lon, ele=ele, time=when))
    return points


def _coordinates_to_points(raw: str) -> list[GeoPoint]:
    """Parse a KML ``<coordinates>`` blob into points.

    The format is whitespace-separated tuples of ``lon,lat[,ele]``. Real files
    wrap and indent them freely, and some exporters leave a trailing comma, so
    the parsing stays forgiving and simply skips anything malformed.
    """
    points: list[GeoPoint] = []
    for chunk in raw.replace("\n", " ").replace("\t", " ").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        lon, lat = parse_float(parts[0]), parse_float(parts[1])
        ele = parse_float(parts[2]) if len(parts) > 2 else None
        if lat is None or lon is None or not _plausible(lat, lon):
            continue
        points.append(GeoPoint(lat=lat, lon=lon, ele=ele))
    return points


#: Google Earth wraps descriptions in CDATA and fills them with markup.
_HTML_TAG = re.compile(r"<[^>]+>")


def _clean_description(text: str) -> str:
    """Reduce a Google Earth description to readable plain text.

    Tags are stripped and entities unescaped so a popup shows "2642 m - closed
    in winter" instead of a wall of markup. The frontend inserts descriptions as
    text rather than HTML, so this is about readability, not safety.
    """
    if not text:
        return ""
    stripped = _HTML_TAG.sub(" ", text)
    collapsed = " ".join(html.unescape(stripped).split())
    return collapsed[:500]


def _plausible(lat: float, lon: float) -> bool:
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    return not (lat == 0.0 and lon == 0.0)
