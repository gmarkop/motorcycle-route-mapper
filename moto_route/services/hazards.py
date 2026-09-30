"""Road closures, roadworks and seasonal gates near the route, from OpenStreetMap.

There is no free pan-European "road closures" API — national road authorities
each run their own feed, most without an open licence. OpenStreetMap is the one
source that covers every country you might ride through with a single query, and
Overpass lets us ask it directly.

What OSM gives you is *durable* closure data: a pass gated shut for the winter,
a bridge under construction for a season, a road signed as no-motor-vehicles.
What it does not give you is this morning's crash on the A8. Treat this layer as
"what my paper map would not have told me", not as a live traffic feed — the
frontend says as much, because an over-trusted safety feature is worse than an
absent one.

The query uses Overpass's ``around`` filter with the route itself as the
geometry, so a 400 km ride searches a 150 m corridor rather than the enormous
bounding box that contains it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Sequence

import httpx

from .. import geo
from ..config import Settings
from ..models import Route
from .cache import TTLCache
from .overpass import (OverpassError, chunk_coordinates, query_header,
                        run_chunked)

log = logging.getLogger(__name__)

#: Coarser than the drawing tolerance: Overpass has to parse every coordinate we
#: send, and a 200 m deviation is irrelevant when the corridor is 150 m wide.

#: Vertices kept per Overpass way when measuring its distance to the route.
_MAX_WAY_VERTICES = 30


@dataclass(slots=True)
class Hazard:
    lat: float
    lon: float
    category: str
    label: str
    detail: str
    osm_type: str
    osm_id: int
    distance_off_route_m: float
    distance_along_route_m: float
    severity: str  # "closed" | "restricted" | "info"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["distance_off_route_m"] = round(self.distance_off_route_m)
        data["distance_along_route_m"] = round(self.distance_along_route_m)
        data["osm_url"] = f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"
        return data


#: The tag filters that make a way interesting, as (label, filter) pairs.
_WAY_FILTERS = (
    '["highway"="construction"]',
    '["highway"]["construction"]',
    '["highway"]["access"="no"]',
    '["highway"]["motor_vehicle"="no"]',
    '["highway"]["seasonal"="yes"]',
    '["highway"]["snowplowing"="no"]',
)
_NODE_FILTERS = (
    '["barrier"]["access"="no"]',
    '["barrier"="lift_gate"]',
)


def build_query(coords: Sequence[geo.LatLon], radius_m: float, limit: int,
                header: str = "[out:json][timeout:90];",
                style: str = "filtered") -> str:
    """Compose the Overpass QL query for a corridor around the route.

    Two shapes, because the obvious optimisation turned out to be a pessimism:

    ``filtered`` (default) applies each tag filter inside its own ``around``.
    That is eight spatial passes, which looks wasteful — but each one is
    answered from the tag index and returns a handful of ways.

    ``grouped`` walks the corridor twice into named sets and filters those. Two
    passes instead of eight, and a much smaller query, but it materialises
    *every* road in the corridor before any filter applies. In open country
    that is nothing; through a town it is thousands of ways, and a chunk that
    ``filtered`` handles in seconds times out repeatedly.

    Measured on a 389 km Greek route: grouped failed its first chunk three times
    over, while the same corridor had been answered whole in 39.5s by filtered.
    """
    joined = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in coords)
    around = f"around:{int(radius_m)},{joined}"

    if style == "grouped":
        body = (f'way({around})["highway"]->.roads;\n'
                f'node({around})["barrier"]->.gates;\n(\n'
                + "".join(f"  way.roads{f};\n" for f in _WAY_FILTERS)
                + "".join(f"  node.gates{f};\n" for f in _NODE_FILTERS)
                + ");")
    else:
        body = ("(\n"
                + "".join(f"  way({around}){f};\n" for f in _WAY_FILTERS)
                + "".join(f"  node({around}){f};\n" for f in _NODE_FILTERS)
                + ");")

    return f"""{header}
{body}
out geom {limit};
"""


async def find_hazards(
    route: Route,
    settings: Settings,
    client: httpx.AsyncClient,
    cache: TTLCache,
) -> dict[str, Any]:
    route_points = route.all_latlon
    if len(route_points) < 2:
        return {"available": False, "reason": "Route has no line to search along.", "hazards": []}
    if settings.offline:
        return {"available": False, "reason": "Offline mode is enabled.", "hazards": []}

    query_coords = _query_coordinates(route_points, settings)
    chunks = chunk_coordinates(query_coords, settings.overpass_max_points)

    def build(chunk):
        return build_query(chunk, settings.hazard_corridor_m, settings.max_hazards,
                           header=query_header(settings),
                           style=settings.overpass_query_style)

    # Cache on every chunk's query, so the key changes when the route does.
    cache_key = "|".join(build(chunk) for chunk in chunks)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        payload = await run_chunked(chunks, build, settings, client,
                                    coverage_points=query_coords)
    except OverpassError as exc:
        log.warning("Overpass closure query failed: %s", exc)
        stale = cache.get_stale(cache_key)
        if stale is not None:
            return dict(stale, stale=True,
                        reason=f"Showing the last closure data — {exc}")
        return {"available": False, "reason": str(exc), "hazards": []}

    hazards, nearby = _elements_to_hazards(payload.get("elements", []),
                                           route_points, settings)
    note = ("From OpenStreetMap: construction, gates and access restrictions. "
            "Long-lived closures only — not live traffic or today's incidents.")
    if nearby:
        note += (f" {nearby} more {'closure is' if nearby == 1 else 'closures are'} "
                 f"within {int(settings.hazard_corridor_m)} m of the route but "
                 f"on other roads, and not shown.")
    if payload.get("partial"):
        note += (f" {payload['failed_chunks']} of {payload['total_chunks']} "
                 "sections of the route could not be checked in the time "
                 "allowed — press Refresh to try the rest.")

    result = {
        "available": True,
        "stale": False,
        "partial": bool(payload.get("partial")),
        "hazards": [h.to_dict() for h in hazards],
        "corridor_m": settings.hazard_corridor_m,
        "on_route_m": settings.hazard_on_route_m,
        # Found in the corridor but not on the road being ridden. Reported
        # rather than dropped: the panel shows what is on the route, and says
        # how much it is not showing, so a quiet map is a statement rather than
        # an absence of one.
        "nearby": nearby,
        "note": note,
    }
    # A partial answer gets a short TTL: the note above tells the rider to press
    # Refresh to try the rest, and the full six hours would make that a lie.
    cache.set(cache_key, result,
              settings.partial_ttl_s if result["partial"] else settings.hazard_ttl_s)
    return result


def _query_coordinates(route_points: Sequence[geo.LatLon],
                       settings: Settings) -> list[geo.LatLon]:
    """Coordinates whose corridor actually covers the route.

    This used to thin at a fixed 250 m and cap the result at 350 points, which
    is where the closure layer quietly stopped searching most of a long route:
    a 250 m tolerance inside a 150 m corridor leaves real road outside the
    corridor on every curve, and 350 points spread over 389 km sit 1.1 km
    apart, nearly four times too far for 150 m circles to touch.

    Nothing said so. The panel reported the closures it found and no error, and
    a closure in one of the gaps simply did not exist as far as the app was
    concerned. Coverage is worth more queries than that.
    """
    return geo.corridor_points(route_points, settings.hazard_corridor_m)


def _elements_to_hazards(
    elements: Sequence[dict[str, Any]],
    route_points: Sequence[geo.LatLon],
    settings: Settings,
) -> list[Hazard]:
    # Built once for the whole batch. Scanning every segment per vertex is
    # what made this layer block the event loop for the best part of a minute.
    index = geo.RouteIndex(route_points, settings.hazard_corridor_m * 2)
    hazards: list[Hazard] = []
    nearby: list[dict[str, Any]] = []

    for element in elements:
        tags = element.get("tags") or {}
        geometry = _element_geometry(element)
        if not geometry:
            continue

        placement = _project_onto_route(geometry, index)
        if placement is None:
            continue
        point, off_route_m, along_m = placement

        # Overpass measures its corridor from the simplified query line; re-check
        # against the real route so a shortcut in the query cannot smuggle in a
        # hazard that is actually far away.
        if off_route_m > settings.hazard_corridor_m * 2:
            continue

        # Searched wide, shown narrow -- the same shape the POI layer uses. The
        # search has to be generous because the query line is simplified and a
        # recorded track wanders, but a closure on the next street over is not
        # a closure on this ride, and a panel full of them is one nobody reads.
        # Counted rather than dropped: see `nearby` in the payload.
        if (off_route_m > settings.hazard_on_route_m
                or not _runs_along_route(geometry, index,
                                         settings.hazard_on_route_m)):
            nearby.append(element)
            continue

        category, label, severity = _classify(tags)
        hazards.append(
            Hazard(
                lat=point[0],
                lon=point[1],
                category=category,
                label=label,
                detail=_describe(tags),
                osm_type=str(element.get("type", "way")),
                osm_id=int(element.get("id", 0)),
                distance_off_route_m=off_route_m,
                distance_along_route_m=along_m,
                severity=severity,
            )
        )

    hazards.sort(key=lambda h: h.distance_along_route_m)
    return hazards[: settings.max_hazards], len(nearby)


def _element_geometry(element: dict[str, Any]) -> list[geo.LatLon]:
    """Coordinates of an Overpass element, thinned for a way."""
    if element.get("type") == "node":
        lat, lon = element.get("lat"), element.get("lon")
        return [(float(lat), float(lon))] if lat is not None and lon is not None else []

    raw = element.get("geometry") or []
    coords = [
        (float(p["lat"]), float(p["lon"]))
        for p in raw
        if isinstance(p, dict) and "lat" in p and "lon" in p
    ]
    if len(coords) <= _MAX_WAY_VERTICES:
        return coords
    # A closed road can be tens of kilometres long; an evenly spaced sample is
    # enough to find where it meets our route, and keeps the projection cheap.
    step = max(1, len(coords) // (_MAX_WAY_VERTICES - 1))
    sampled = coords[::step][: _MAX_WAY_VERTICES - 1]
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])
    return sampled


#: A way has to run this far inside the on-route distance before it counts as
#: the road you are riding. A side road crossing at a junction contributes only
#: its first segment or two; a closure on your own road contributes hundreds of
#: metres. Short ways are judged by the fraction instead, so a closed 80 m
#: bridge on the route is not dismissed for being short.
ON_ROUTE_RUN_M = 200.0
ON_ROUTE_FRACTION = 0.6


def _project_onto_route(
    geometry: Sequence[geo.LatLon],
    index: geo.RouteIndex,
) -> tuple[geo.LatLon, float, float] | None:
    """Find where a hazard touches the route.

    Returns the hazard vertex closest to the route, how far off-route it is, and
    how far along the route that meeting point lies — which is what turns a map
    pin into "roadworks at km 143".
    """
    best: tuple[geo.LatLon, float, float] | None = None

    for vertex in geometry:
        # One index lookup gives both answers. The old pair -- a full polyline
        # scan for the distance, then a full scan again for the nearest vertex
        # -- was two passes over every point of the route, per vertex, per
        # hazard.
        off, along = index.project(vertex)
        if best is None or off < best[1]:
            best = (vertex, off, along)

    return best


def _runs_along_route(
    geometry: Sequence[geo.LatLon],
    index: geo.RouteIndex,
    on_route_m: float,
) -> bool:
    """Is this closure on the road being ridden, or does it merely touch it?

    The distinction the closest-vertex test cannot make, and the one that fills
    the panel with things that are not on the route. A closed side street
    meeting the route at a junction has a vertex *on* the route -- zero metres
    off it -- and reads exactly like a closure of the road you are riding. What
    separates them is not how close the nearest point is but how much of the
    way keeps company with the route: a junction contributes a segment, your
    own road contributes its length.

    A single-vertex hazard -- a gate, a barrier -- has no length to measure, so
    it is judged by distance alone and this is not asked of it.
    """
    if len(geometry) < 2:
        return True

    near = [index.project(vertex)[0] <= on_route_m for vertex in geometry]
    alongside = total = 0.0
    for (a, b), (a_near, b_near) in zip(zip(geometry, geometry[1:]),
                                        zip(near, near[1:])):
        length = geo.haversine_m(a, b)
        total += length
        if a_near and b_near:
            alongside += length

    return alongside >= min(ON_ROUTE_RUN_M, total * ON_ROUTE_FRACTION)


def _classify(tags: dict[str, str]) -> tuple[str, str, str]:
    """Map OSM tags onto a category, a human label and a severity."""
    if tags.get("highway") == "construction" or "construction" in tags:
        what = tags.get("construction") or tags.get("construction:highway") or "road"
        return "construction", f"Under construction ({what})", "closed"
    if tags.get("access") == "no" and "barrier" in tags:
        return "gate", f"Closed {tags.get('barrier', 'barrier').replace('_', ' ')}", "closed"
    if tags.get("barrier") == "lift_gate":
        return "gate", "Lift gate", "restricted"
    if tags.get("motor_vehicle") == "no":
        return "restricted", "No motor vehicles", "closed"
    if tags.get("access") == "no":
        return "restricted", "Access forbidden", "closed"
    if tags.get("seasonal") == "yes" or tags.get("snowplowing") == "no":
        return "seasonal", "Seasonal road — may be shut in winter", "info"
    return "other", "Restriction", "info"


def _describe(tags: dict[str, str]) -> str:
    """A short, readable line from the tags a rider would care about."""
    interesting = ("name", "ref", "opening_hours", "description", "note",
                   "check_date", "seasonal", "surface")
    parts = [f"{key}: {tags[key]}" for key in interesting if tags.get(key)]
    return " · ".join(parts)[:300]
