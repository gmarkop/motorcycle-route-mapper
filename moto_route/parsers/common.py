"""Shared XML plumbing for the GPX and KML parsers.

Two problems show up in every real-world route file and are solved once here:

1. **Namespaces.** GPX 1.1 uses ``http://www.topografix.com/GPX/1/1``, GPX 1.0
   uses ``.../1/0``, KML 2.2 uses ``http://www.opengis.net/kml/2.2`` and Google
   Earth adds ``gx:`` on top. ElementTree reports tags as
   ``{namespace}localname``, so matching on the local name alone lets one parser
   read every dialect instead of hard-coding namespace URLs.

2. **Hostile or broken input.** ``xml.etree`` will happily expand nested entity
   declarations ("billion laughs") until the process runs out of memory. A route
   file never needs a DTD, so we reject one outright.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator
from xml.etree import ElementTree as ET


class RouteParseError(ValueError):
    """Raised when a file is not a route we can read."""


#: Refuse anything larger than this (uncompressed). A 50 MB GPX is around two
#: million track points — far past anything a real ride produces.
MAX_XML_BYTES = 50 * 1024 * 1024

_ENTITY_DECL = re.compile(rb"<!ENTITY", re.IGNORECASE)
_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def parse_xml(data: bytes) -> ET.Element:
    """Parse bytes into an XML root element, refusing unsafe documents."""
    if len(data) > MAX_XML_BYTES:
        raise RouteParseError(
            f"File is larger than the {MAX_XML_BYTES // (1024 * 1024)} MB limit."
        )
    head = data[:4096]
    if _DOCTYPE.search(head) or _ENTITY_DECL.search(data[: 64 * 1024]):
        raise RouteParseError(
            "This file declares a DTD or XML entities, which route files never "
            "need and which can be used to exhaust memory. Refusing to parse it."
        )
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise RouteParseError(f"File is not valid XML: {exc}") from exc


def localname(elem: ET.Element) -> str:
    """``{http://...}trkpt`` -> ``trkpt``."""
    tag = elem.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.rsplit("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def children(parent: ET.Element, name: str) -> Iterator[ET.Element]:
    """Direct children whose local name matches (case-insensitively)."""
    wanted = name.lower()
    for child in parent:
        if localname(child).lower() == wanted:
            yield child


def first_child(parent: ET.Element, name: str) -> ET.Element | None:
    return next(children(parent, name), None)


def descendants(parent: ET.Element, name: str) -> Iterator[ET.Element]:
    """Every descendant with this local name, at any depth."""
    wanted = name.lower()
    for elem in parent.iter():
        if localname(elem).lower() == wanted:
            yield elem


def text_of(parent: ET.Element, name: str, default: str = "") -> str:
    """Trimmed text of the first matching direct child."""
    child = first_child(parent, name)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def parse_time(value: str | None) -> datetime | None:
    """Parse the ISO-8601 timestamps GPX writes, e.g. ``2024-06-01T08:15:00Z``.

    Returns ``None`` rather than raising: a malformed timestamp on one track
    point is no reason to reject an otherwise good ride.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
