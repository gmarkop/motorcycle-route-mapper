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
from .services.daylight import light_at

GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"

#: Waypoint symbols Garmin devices recognise. An unknown name falls back to a
#: plain dot on the device rather than failing, but these are the useful ones.
SYMBOLS = {
    "fuel": "Gas Station",
    "cafe": "Restaurant",
    "viewpoint": "Scenic Area",
    "accommodation": "Lodging",
    "motorcycle_parking": "Parking Area",
    "hazard": "Danger Area",
    "demanding": "Summit",
    "daylight": "Pin, Blue",
    "weather": "Flag, Red",
    "waypoint": "Flag, Blue",
}

#: What each category is called on the device. GPX has no colour for a
#: waypoint -- it is not in the 1.1 schema and Garmin's waypoint extension
#: does not add one -- so the symbol carries the category and the name carries
#: the meaning. On a handlebar-mounted screen that reads better than colour
#: anyway, and it degrades to a plain dot rather than nothing on a device that
#: does not know the symbol.
#: What the export can be asked for, as the query parameter spells them.
#: "fuel" is the planned refuelling stops the tank range depends on;
#: "fuel_all" is every other pump as well, which on a long route is a hundred
#: more markers and is a separate decision from wanting the plan.
EXPORTABLE = frozenset({
    "waypoints", "fuel", "fuel_all", "cafe", "viewpoint", "accommodation",
    "motorcycle_parking", "hazards", "demanding", "weather", "daylight",
})

#: What an export with nothing asked for contains. Everything except cafes:
#: every cafe within 300 m of a 400 km ride is not a useful device waypoint,
#: and there is nowhere in a GPX to put the opening hours that would make one
#: worth choosing. Selectable now, rather than refused -- but not by default,
#: because that is a change nobody asked for.
DEFAULT_INCLUDE = EXPORTABLE - {"cafe"}

LABELS = {
    "fuel": "Fuel",
    # Plain "Cafe": this one is read off a device, and the accented form
    # depends on the font the device happens to ship.
    "cafe": "Cafe",
    "viewpoint": "Viewpoint",
    "accommodation": "Hotel",
    "motorcycle_parking": "Parking",
}


def build_gpx(
    route: Route,
    weather: dict[str, Any] | None = None,
    hazards: dict[str, Any] | None = None,
    pois: dict[str, Any] | None = None,
    demanding: list[dict[str, Any]] | None = None,
    *,
    daylight: dict[str, Any] | None = None,
    include: frozenset[str] | set[str] | None = None,
    include_shaping_points: bool = False,
    include_track: bool = True,
    include_waypoints: bool = True,
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

    if include_waypoints:
        for waypoint in _collect_waypoints(route, weather, hazards, pois, demanding,
                                           daylight, include_shaping_points,
                                           DEFAULT_INCLUDE if include is None else include):
            _write_waypoint(root, waypoint)

    # A file holding only the stops is how a navigation app is stopped from
    # guessing. Given both waypoints and a track, several -- Scenic among them
    # -- assume the waypoints are the route's via points, because that is the
    # shape Garmin route files have, and renumber them "1, 2, 3" in place of
    # their names. With no track in the file there is nothing to attach them
    # to, so they arrive as what they are.
    for index, line in enumerate(route.lines if include_track else []):
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
    demanding: list[dict[str, Any]] | None,
    daylight: dict[str, Any] | None,
    include_shaping_points: bool,
    include: frozenset[str] | set[str],
) -> list[dict[str, Any]]:
    """Gather everything worth marking, in route order.

    ``include`` names the categories wanted. A device screen is small and a
    long route carries a hundred pumps, so which of these is worth having is
    the rider's call, not a fixed list.
    """
    collected: list[dict[str, Any]] = []

    for waypoint in route.waypoints if "waypoints" in include else []:
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
    for point in _entries(weather, "points") if "weather" in include else []:
        warnings = point.get("warnings") or []
        if not warnings:
            continue
        distance_km = (point.get("distance_m") or 0) / 1000
        collected.append({
            "lat": point["lat"],
            "lon": point["lon"],
            "name": f"Weather km {distance_km:.0f}: {point.get('description', '')}".strip(),
            "desc": ("; ".join(warnings)
                     + f" (rideability {point.get('rideability', '?')}/100)."
                     + _forecast_stamp(point.get("eta"), (weather or {}).get("fetched_at"))),
            "sym": SYMBOLS["weather"],
            "type": "weather",
            "along": point.get("distance_m") or 0.0,
        })

    for hazard in _entries(hazards, "hazards") if "hazards" in include else []:
        # Every other category names itself; hazards did not, so a barrier
        # arrived on the device as "No motor vehicles" -- indistinguishable
        # from a place, when it is the reason the road is shut.
        severity = hazard.get("severity", "info")
        prefix = {"closed": "Closed", "restricted": "Restricted"}.get(severity, "Roadworks")
        label = hazard.get("label") or "Closure"
        collected.append({
            "lat": hazard["lat"],
            "lon": hazard["lon"],
            "name": label if label.lower().startswith(prefix.lower()) else f"{prefix}: {label}",
            # The OSM link belongs in the panel, where it can be clicked. On a
            # phone in a tank bag it is a line of unreadable digits pushing the
            # part that matters off the screen.
            "desc": hazard.get("detail", ""),
            "sym": SYMBOLS["hazard"],
            "type": f"hazard:{hazard.get('severity', 'info')}",
            "along": hazard.get("distance_along_route_m") or 0.0,
        })

    # Planned refuelling stops are numbered in riding order, so "Fuel stop 2"
    # on the device means the second one the plan depends on -- not the second
    # pump you happen to pass.
    stop_number = 0
    for poi in sorted(_entries(pois, "pois"),
                      key=lambda item: item.get("distance_along_route_m") or 0.0):
        category = poi.get("category", "waypoint")
        # Every cafe within 300 m of a 400 km ride is not a useful device
        # waypoint, and there is nowhere to put the opening hours that would
        # make one worth choosing.
        if category not in include:
            continue
        # Wanting the plan is not the same as wanting every pump on the route.
        if category == "fuel" and not poi.get("recommended") and "fuel_all" not in include:
            continue

        if category == "fuel" and poi.get("recommended"):
            stop_number += 1
            label = f"Fuel stop {stop_number}"
        else:
            # Every other station still goes, named plainly. The failure that
            # actually happens on the road is a planned stop being shut, and
            # then what you want is the next pump, not a tidier screen.
            label = LABELS.get(category, "Waypoint")

        off_route = poi.get("distance_off_route_m")
        detail = poi.get("detail", "")
        if off_route is not None and off_route > 50:
            # The device cannot show the panel's off-route line, and a station
            # a kilometre up a side road is a different decision.
            away = (f"{off_route / 1000:.1f} km" if off_route >= 1000
                    else f"{round(off_route / 10) * 10:.0f} m")
            detail = f"{detail} · {away} off route".strip(" ·")

        # "Hotel: Hotel Meteora" reads like a mistake on a small screen, and
        # the symbol already says which category it is.
        poi_name = (poi.get("name") or "").strip()
        titled = (f"{label}: {poi_name}".strip(": ")
                  if not poi_name.lower().startswith(label.lower())
                  else poi_name)

        collected.append({
            "lat": poi["lat"],
            "lon": poi["lon"],
            "name": titled,
            "desc": detail,
            "sym": SYMBOLS.get(category, SYMBOLS["waypoint"]),
            "type": category,
            "along": poi.get("distance_along_route_m") or 0.0,
        })

    for stretch in (demanding or []) if "demanding" in include else []:
        if stretch.get("from_lat") is None or stretch.get("from_lon") is None:
            continue
        km = (stretch.get("length_m") or 0) / 1000.0
        slope = stretch.get("gradient_pct") or 0.0
        way = "down" if stretch.get("descending") else "up"
        collected.append({
            "lat": stretch["from_lat"],
            "lon": stretch["from_lon"],
            "name": f"Twisty & steep: {km:.1f} km {way}",
            "desc": (f"{abs(slope):.0f}% gradient, "
                     f"{stretch.get('curviness', 0):.0f}°/km. Starts here."
                     + _light_note(daylight, stretch.get("from_m"), stretch.get("to_m"))),
            "sym": SYMBOLS["demanding"],
            "type": "demanding",
            "along": stretch.get("from_m") or 0.0,
        })

    changes = (daylight or {}).get("changes") or []
    for change in changes if "daylight" in include and (daylight or {}).get("available") else []:
        when = _clock(change.get("eta"), day=False)
        collected.append({
            "lat": change["lat"],
            "lon": change["lon"],
            "name": _DAYLIGHT_NAMES.get(change["event"], change["event"]).format(when=when),
            "desc": (f"Expected at km {change['distance_m'] / 1000:.0f}, {when}"
                     f" ({_zone(change.get('eta'))}), at the speed and start time planned."),
            "sym": SYMBOLS["daylight"],
            "type": "daylight",
            "along": change["distance_m"],
        })

    collected.sort(key=lambda item: item["along"])
    return collected


#: How each change of light is named on the device. "Dark from" rather than
#: "Civil dusk": the second is correct and means nothing on a handlebar.
_DAYLIGHT_NAMES = {
    "sunset": "Sunset {when}",
    "dark": "Dark from {when}",
    "first light": "First light {when}",
    "sunrise": "Sunrise {when}",
}

#: Worst first: a stretch that ends in the dark is a dark stretch, whatever the
#: light was when it began.
_LIGHT_NOTES = (("night", " In the dark."), ("dusk", " After sunset."),
                ("dawn", " Before sunrise."))


def _light_note(daylight: dict[str, Any] | None, start_m, end_m) -> str:
    """" After sunset." and the like, for a stretch that is not in daylight."""
    seen = {light_at(daylight, start_m or 0.0), light_at(daylight, end_m or start_m or 0.0)}
    for light, note in _LIGHT_NOTES:
        if light in seen:
            return note
    return ""


def _forecast_stamp(eta: str | None, fetched_at: str | None) -> str:
    """" Forecast for Fri 14:20, checked Thu 21:05 (EEST)."

    A marker on a phone mid-ride looks exactly as current a week after export
    as it did the evening it was made. The hour it is for says whether you are
    early or late against it; the hour it was fetched says how far to trust it.
    """
    target = _clock(eta)
    if target is None:
        return ""
    checked = _clock(fetched_at)
    zone = _zone(eta)
    if checked is None:
        return f" Forecast for {target} ({zone})."
    return f" Forecast for {target}, checked {checked} ({zone})."


def _clock(iso: str | None, *, day: bool = True) -> str | None:
    """An ISO time on the server's clock, the way a rider reads one.

    The server's zone, because the export has no other to go on -- the browser's
    is not sent, and the box this runs on sits in the rider's own country. The
    zone is printed beside it so that is never a silent assumption.
    """
    local = _local(iso)
    if local is None:
        return None
    return local.strftime("%a %H:%M" if day else "%H:%M")


def _zone(iso: str | None) -> str:
    local = _local(iso)
    return (local.tzname() or "local") if local else "local"


def _local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone()


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


def suggested_filename(route: Route, suffix: str = "enriched") -> str:
    """A filesystem-safe name derived from the route, for the download header."""
    base = "".join(
        char if char.isalnum() or char in " -_" else "-"
        for char in (route.name or "route")
    ).strip() or "route"
    return f"{base[:60].replace(' ', '_')}_{suffix}.gpx"
