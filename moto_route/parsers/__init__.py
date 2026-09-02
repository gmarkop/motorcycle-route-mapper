"""Format detection and dispatch.

The caller hands over raw bytes and (optionally) the original filename; this
module decides which reader to use. Content is trusted over the extension,
because a file called ``route.gpx`` that a phone re-encoded as KML is common
enough to be worth handling.
"""

from __future__ import annotations

from ..models import Route
from .common import RouteParseError
from .gpx import parse_gpx
from .kml import parse_kml, parse_kmz

__all__ = ["parse_route_bytes", "detect_format", "RouteParseError",
           "parse_gpx", "parse_kml", "parse_kmz", "SUPPORTED_EXTENSIONS"]

SUPPORTED_EXTENSIONS = (".gpx", ".kml", ".kmz")

#: Local file header of every zip archive, and therefore of every KMZ.
_ZIP_MAGIC = b"PK\x03\x04"


def detect_format(data: bytes, filename: str = "") -> str:
    """Return ``"gpx"``, ``"kml"`` or ``"kmz"``, or raise :class:`RouteParseError`."""
    if data[:4] == _ZIP_MAGIC:
        return "kmz"

    # Look at the opening of the document only: a KML placemark description can
    # contain the literal text "gpx", which would fool a whole-file search.
    head = data[:4096].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if b"<gpx" in head:
        return "gpx"
    if b"<kml" in head:
        return "kml"

    lowered = filename.lower()
    for extension in SUPPORTED_EXTENSIONS:
        if lowered.endswith(extension):
            return extension.lstrip(".")

    raise RouteParseError(
        "Unrecognised file. Expected a GPX, KML or KMZ route "
        f"(got {len(data)} bytes starting with {head[:40]!r})."
    )


def parse_route_bytes(data: bytes, filename: str = "") -> Route:
    """Parse an uploaded route file into a :class:`~moto_route.models.Route`."""
    if not data:
        raise RouteParseError("The uploaded file is empty.")

    fmt = detect_format(data, filename)
    route = {"gpx": parse_gpx, "kml": parse_kml, "kmz": parse_kmz}[fmt](data)

    if filename and not route.name.strip():
        route.name = filename.rsplit("/", 1)[-1]
    route.metadata.setdefault("filename", filename)
    route.annotate_waypoint_distances()
    return route
