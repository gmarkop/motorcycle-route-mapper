"""Write the enriched ride back out as GPX.

The point of this is the round trip. You plan at the kitchen table, the app
works out that it will be hammering down at km 210, that the pass at km 62 is
gated shut, and that there is no fuel for 140 km after Sella — and then all of
that stays on the laptop while you ride off with the original file on the
Garmin.

So the export folds those findings back into the file as ordinary waypoints.
Every device that reads GPX shows waypoints; none of them need to understand
anything specific to this app. Symbols use the names Garmin recognises so they
come out as the right icon rather than a generic dot.

Element order matters: the GPX 1.1 schema is a sequence, so ``metadata`` then
``wpt`` then ``trk``. Emitting them out of order produces a file that some
parsers accept and others silently reject.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from . import __version__
from .models import Route

GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"

#: Waypoint symbols Garmin devices recognise. An unknown name falls back to a
#: plain dot on the device rather than failing, but these are the useful ones.
SYMBOLS = {
    "fuel": "Gas Station",
    "cafe": "Restaurant",
    "viewpoint": "Scenic Area",
    "hazard": "Danger Area",
    "weather": "Flag, Red",
    "waypoint": "Flag, Blue",
}


def build_gpx(
    route: Route,
    weather: dict[str, Any] | None = None,
    hazards: dict[str, Any] | None = None,
    pois: dict[str, Any] | None = None,
    *,
    include_shaping_points: bool = False,
) -> bytes:
    """Serialise the route plus whatever enrichment was supplied.

    Each enrichment argument is the dict the matching service returns, so the
    caller can pass through exactly what it already fetched — and pass ``None``
    for any layer that failed or was never requested.
    """
    root = ET.Element("gpx", {
        "version": "1.1",
        "creator": f"moto-route-mapper/{__version__}",
        "xmlns": GPX_NAMESPACE,
    })

    metadata = ET.SubElement(root, "metadata")
    _text(metadata, "name", route.name or "Route")
    _text(metadata, "desc", _summary_line(route, weather, hazards, pois))
    _text(metadata, "time", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    for waypoint in _collect_waypoints(route, weather, hazards, pois, include_shaping_points):
        _write_waypoint(root, waypoint)

    for index, line in enumerate(route.lines):
        if not line:
            continue
        track = ET.SubElement(root, "trk")
        name = route.name or "Track"
        _text(track, "name", name if len(route.lines) == 1 else f"{name} ({index + 1})")
        segment = ET.SubElement(track, "trkseg")
        for point in line:
            attrs = {"lat": f"{point.lat:.6f}", "lon": f"{point.lon:.6f}"}
            trkpt = ET.SubElement(segment, "trkpt", attrs)
            if point.ele is not None:
                _text(trkpt, "ele", f"{point.ele:.1f}")
            if point.time is not None:
                _text(trkpt, "time", point.time.strftime("%Y-%m-%dT%H:%M:%SZ"))

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'.encode("utf-8")


def _collect_waypoints(
    route: Route,
    weather: dict[str, Any] | None,
    hazards: dict[str, Any] | None,
    pois: dict[str, Any] | None,
    include_shaping_points: bool,
) -> list[dict[str, Any]]:
    """Gather everything worth marking, in route order."""
    collected: list[dict[str, Any]] = []

    for waypoint in route.waypoints:
        # Shaping points exist to bend the line onto a road; exporting them as
        # stops would clutter the device with places you never chose to visit.
        if waypoint.kind == "shaping" and not include_shaping_points:
            continue
        collected.append({
            "lat": waypoint.lat,
            "lon": waypoint.lon,
            "name": waypoint.name or "Waypoint",
            "desc": waypoint.description,
            "sym": waypoint.symbol or SYMBOLS["waypoint"],
            "type": waypoint.kind,
            "along": waypoint.distance_m or 0.0,
        })

    # Only forecast points that actually carry a warning are worth a marker —
    # a waypoint saying "18 °C and fine" is noise on a device screen.
    for point in _entries(weather, "points"):
        warnings = point.get("warnings") or []
        if not warnings:
            continue
        distance_km = (point.get("distance_m") or 0) / 1000
        collected.append({
            "lat": point["lat"],
            "lon": point["lon"],
            "name": f"Weather km {distance_km:.0f}: {point.get('description', '')}".strip(),
            "desc": "; ".join(warnings) + f" (rideability {point.get('rideability', '?')}/100)",
            "sym": SYMBOLS["weather"],
            "type": "weather",
            "along": point.get("distance_m") or 0.0,
        })

    for hazard in _entries(hazards, "hazards"):
        collected.append({
            "lat": hazard["lat"],
            "lon": hazard["lon"],
            "name": hazard.get("label", "Closure"),
            "desc": " ".join(filter(None, [hazard.get("detail", ""), hazard.get("osm_url", "")])),
            "sym": SYMBOLS["hazard"],
            "type": f"hazard:{hazard.get('severity', 'info')}",
            "along": hazard.get("distance_along_route_m") or 0.0,
        })

    for poi in _entries(pois, "pois"):
        category = poi.get("category", "waypoint")
        # Every cafe within 300 m of a 400 km ride is not a useful device
        # waypoint. Fuel that the plan actually depends on is.
        if category == "fuel" and not poi.get("recommended"):
            continue
        if category == "cafe":
            continue
        prefix = "Fuel stop" if category == "fuel" else "Viewpoint"
        collected.append({
            "lat": poi["lat"],
            "lon": poi["lon"],
            "name": f"{prefix}: {poi.get('name', '')}".strip(": "),
            "desc": poi.get("detail", ""),
            "sym": SYMBOLS.get(category, SYMBOLS["waypoint"]),
            "type": category,
            "along": poi.get("distance_along_route_m") or 0.0,
        })

    collected.sort(key=lambda item: item["along"])
    return collected


def _entries(payload: dict[str, Any] | None, key: str) -> Iterable[dict[str, Any]]:
    """Items from a service response, tolerating a failed or absent layer."""
    if not payload or not payload.get("available"):
        return []
    values = payload.get(key)
    return values if isinstance(values, list) else []


def _write_waypoint(root: ET.Element, waypoint: dict[str, Any]) -> None:
    element = ET.SubElement(root, "wpt", {
        "lat": f"{waypoint['lat']:.6f}",
        "lon": f"{waypoint['lon']:.6f}",
    })
    # ElementTree escapes text content, so a waypoint named `Cafe & Bar <2>`
    # cannot break the document.
    _text(element, "name", waypoint["name"][:100])
    if waypoint.get("desc"):
        _text(element, "desc", waypoint["desc"][:500])
    _text(element, "sym", waypoint["sym"])
    _text(element, "type", waypoint["type"])


def _summary_line(
    route: Route,
    weather: dict[str, Any] | None,
    hazards: dict[str, Any] | None,
    pois: dict[str, Any] | None,
) -> str:
    """One line in the file metadata saying what was folded in, and when."""
    parts = [f"{route.distance_m / 1000:.1f} km"]

    if weather and weather.get("available"):
        summary = weather.get("summary") or {}
        if summary.get("verdict"):
            parts.append(f"Weather: {summary['verdict']}")
    if hazards and hazards.get("available"):
        parts.append(f"{len(hazards.get('hazards') or [])} mapped closures")
    if pois and pois.get("available"):
        plan = pois.get("fuel_plan") or {}
        if plan.get("gaps"):
            longest = max(gap["length_km"] for gap in plan["gaps"])
            parts.append(f"Longest stretch without fuel: {longest:.0f} km")

    parts.append(f"Enriched {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return " · ".join(parts)


def _text(parent: ET.Element, tag: str, value: str) -> None:
    if value:
        ET.SubElement(parent, tag).text = value


def suggested_filename(route: Route) -> str:
    """A filesystem-safe name derived from the route, for the download header."""
    base = "".join(
        char if char.isalnum() or char in " -_" else "-"
        for char in (route.name or "route")
    ).strip() or "route"
    return f"{base[:60].replace(' ', '_')}_enriched.gpx"
