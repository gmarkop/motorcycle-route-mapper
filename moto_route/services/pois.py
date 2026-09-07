"""Fuel, coffee and viewpoints along the route — and whether you can actually
make it between the fuel stops.

The listing part is ordinary: ask Overpass for `amenity=fuel`, `amenity=cafe`
and `tourism=viewpoint` inside a corridor of the route, place each one at its
position along the ride.

The part worth having is the range planning. A motorcycle carries 15-20 litres,
not 60, so "next fuel in 140 km" is a real problem that no car navigation app
thinks to warn you about — and in the Alps or rural Spain the gap between
stations genuinely exceeds a tank. So the fuel stops are run through a greedy
plan: ride as far as the usable range allows, stop at the last station you can
still reach, repeat. If no station is reachable, that is a gap, and the app says
so before you leave rather than when the fuel light comes on.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

import httpx

from .. import geo
from ..config import Settings
from ..models import Route
from .cache import TTLCache
from .overpass import (OverpassError, chunk_coordinates, query_header,
                        run_chunked)

log = logging.getLogger(__name__)

_QUERY_SIMPLIFY_M = 250.0
_MAX_QUERY_POINTS = 350


@dataclass(slots=True)
class Poi:
    lat: float
    lon: float
    category: str          # "fuel" | "cafe" | "viewpoint"
    name: str
    detail: str
    osm_type: str
    osm_id: int
    distance_along_route_m: float
    distance_off_route_m: float
    #: Fuel only: part of the recommended refuelling plan.
    recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["distance_along_route_m"] = round(self.distance_along_route_m)
        data["distance_off_route_m"] = round(self.distance_off_route_m)
        data["osm_url"] = f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"
        return data


@dataclass(slots=True)
class FuelPlan:
    """The refuelling plan and, more importantly, where it breaks down."""

    usable_range_m: float
    stops: list[int] = field(default_factory=list)      # indices into the fuel list
    gaps: list[dict[str, Any]] = field(default_factory=list)
    reachable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "usable_range_km": round(self.usable_range_m / 1000, 1),
            "stop_count": len(self.stops),
            "gaps": self.gaps,
            "reachable": self.reachable,
        }


def build_query(coords: Sequence[geo.LatLon], settings: Settings) -> str:
    """Overpass query for the three categories of stop.

    ``nwr`` covers nodes, ways and relations in one go: a motorway services is
    usually mapped as a way or a relation, not a node, and querying only nodes
    silently misses exactly the big stations you most want to know about.

    All three categories are fetched at the widest corridor — fuel's — in two
    passes rather than three at their own radii. Walking the corridor is what
    costs; the per-category corridor is then applied in
    :func:`_elements_to_pois`, which was already re-checking distances against
    the real route anyway.
    """
    joined = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in coords)
    widest = max(settings.fuel_corridor_m, settings.cafe_corridor_m,
                 settings.viewpoint_corridor_m)
    around = f"around:{int(widest)},{joined}"

    return f"""{query_header(settings)}
(
  nwr({around})["amenity"~"^(fuel|cafe)$"];
  nwr({around})["tourism"="viewpoint"];
);
out center {settings.max_pois};
"""


async def find_pois(
    route: Route,
    settings: Settings,
    client: httpx.AsyncClient,
    cache: TTLCache,
    tank_range_km: float | None = None,
) -> dict[str, Any]:
    route_points = route.all_latlon
    if len(route_points) < 2:
        return {"available": False, "reason": "Route has no line to search along.", "pois": []}
    if settings.offline:
        return {"available": False, "reason": "Offline mode is enabled.", "pois": []}

    chunks = chunk_coordinates(_query_coordinates(route_points),
                               settings.overpass_max_points)

    def build(chunk):
        return build_query(chunk, settings)

    cache_key = "|".join(build(chunk) for chunk in chunks)
    cached = cache.get(cache_key)
    if cached is None:
        try:
            widest = max(settings.fuel_corridor_m, settings.cafe_corridor_m,
                         settings.viewpoint_corridor_m)
            bounds = geo.bounding_box(_query_coordinates(route_points),
                                      margin_m=widest)
            cached = await run_chunked(chunks, build, settings, client,
                                       bounds=bounds)
        except OverpassError as exc:
            log.warning("Overpass POI query failed: %s", exc)
            stale = cache.get_stale(cache_key)
            if stale is None:
                return {"available": False, "reason": str(exc), "pois": []}
            cached = stale
        else:
            # Short TTL for a partial answer, so "press Refresh to try the
            # rest" actually reaches Overpass instead of replaying the gaps.
            cache.set(cache_key, cached,
                      settings.partial_ttl_s if cached.get("partial")
                      else settings.hazard_ttl_s)

    pois = _elements_to_pois(cached.get("elements", []), route_points, settings)

    # Range planning runs over the fuel stops only, in route order.
    fuel = [p for p in pois if p.category == "fuel"]
    range_km = tank_range_km if tank_range_km and tank_range_km > 0 else settings.tank_range_km
    plan = plan_fuel_stops(
        [p.distance_along_route_m for p in fuel],
        route_length_m=route.distance_m,
        range_m=range_km * 1000.0,
        reserve_fraction=settings.fuel_reserve_fraction,
    )
    for index in plan.stops:
        fuel[index].recommended = True

    return {
        "available": True,
        "pois": [p.to_dict() for p in pois],
        "counts": {
            category: sum(1 for p in pois if p.category == category)
            for category in ("fuel", "cafe", "viewpoint")
        },
        "fuel_plan": plan.to_dict(),
        "partial": bool(cached.get("partial")),
        "note": (
            "Fuel, cafes and viewpoints from OpenStreetMap. Opening hours are "
            "whatever the map says, which on a rural pump may be nothing at all."
            + (f" {cached['failed_chunks']} of {cached['total_chunks']} sections "
               "of the route could not be checked in the time allowed — press "
               "Refresh to try the rest."
               if cached.get("partial") else "")
        ),
    }


def plan_fuel_stops(
    fuel_distances_m: Sequence[float],
    route_length_m: float,
    range_m: float,
    reserve_fraction: float = 0.15,
) -> FuelPlan:
    """Greedily choose refuelling stops, and report any stretch you cannot cross.

    The rule is "ride to the last station still within range, fill up, repeat" —
    which minimises the number of stops. Starting with a full tank is assumed;
    the reserve is held back so the plan never depends on the last few hundred
    metres of tank.

    A gap is any stretch where the next station is beyond reach. It is reported
    rather than silently skipped, because that is the one output that changes
    what you do before setting off.
    """
    usable = max(range_m * (1.0 - reserve_fraction), 1.0)
    plan = FuelPlan(usable_range_m=usable)

    # Keep the original index alongside the distance so the caller can mark the
    # chosen stations, while we iterate in route order.
    ordered = sorted(enumerate(fuel_distances_m), key=lambda pair: pair[1])
    position = 0.0

    while position + usable < route_length_m:
        furthest_reachable: tuple[int, float] | None = None
        next_ahead: tuple[int, float] | None = None

        for index, distance in ordered:
            if distance <= position:
                continue
            if next_ahead is None:
                next_ahead = (index, distance)
            if distance > position + usable:
                break
            furthest_reachable = (index, distance)

        if furthest_reachable is not None:
            plan.stops.append(furthest_reachable[0])
            position = furthest_reachable[1]
            continue

        # Nothing within range. Name the far end of the gap so the warning says
        # how long the dry stretch is, not merely where it starts.
        gap_end = next_ahead[1] if next_ahead else route_length_m
        plan.gaps.append({
            "from_km": round(position / 1000, 1),
            "to_km": round(gap_end / 1000, 1),
            "length_km": round((gap_end - position) / 1000, 1),
        })
        plan.reachable = False

        if next_ahead is None:
            break

        # Carry on planning past the gap — the rest of the route is still worth
        # knowing about, and the rider may be carrying a can.
        plan.stops.append(next_ahead[0])
        position = next_ahead[1]

    return plan


def _query_coordinates(route_points: Sequence[geo.LatLon]) -> list[geo.LatLon]:
    simplified = geo.simplify(route_points, _QUERY_SIMPLIFY_M)
    if len(simplified) <= _MAX_QUERY_POINTS:
        return simplified
    step = len(simplified) / _MAX_QUERY_POINTS
    picked = [simplified[int(i * step)] for i in range(_MAX_QUERY_POINTS)]
    if picked[-1] != simplified[-1]:
        picked.append(simplified[-1])
    return picked


def _elements_to_pois(
    elements: Sequence[dict[str, Any]],
    route_points: Sequence[geo.LatLon],
    settings: Settings,
) -> list[Poi]:
    cumulative = geo.cumulative_distances(route_points)
    corridor = {
        "fuel": settings.fuel_corridor_m,
        "cafe": settings.cafe_corridor_m,
        "viewpoint": settings.viewpoint_corridor_m,
    }

    pois: list[Poi] = []
    for element in elements:
        tags = element.get("tags") or {}
        category = _categorise(tags)
        if category is None:
            continue

        position = _element_position(element)
        if position is None:
            continue

        off_route, along_route = geo.project_onto_polyline(position, route_points, cumulative)
        # Overpass measured against the simplified query line; re-check against
        # the real one, with slack for the simplification itself.
        if off_route > corridor[category] * 1.5:
            continue

        pois.append(Poi(
            lat=position[0],
            lon=position[1],
            category=category,
            name=tags.get("name") or _default_name(category),
            detail=_describe(tags, category),
            osm_type=str(element.get("type", "node")),
            osm_id=int(element.get("id", 0)),
            distance_along_route_m=along_route,
            distance_off_route_m=off_route,
        ))

    pois.sort(key=lambda p: p.distance_along_route_m)
    return pois[: settings.max_pois]


def _element_position(element: dict[str, Any]) -> geo.LatLon | None:
    """Coordinates of a node, or the centroid Overpass computes for a way."""
    if element.get("lat") is not None and element.get("lon") is not None:
        return (float(element["lat"]), float(element["lon"]))
    centre = element.get("center")
    if isinstance(centre, dict) and "lat" in centre and "lon" in centre:
        return (float(centre["lat"]), float(centre["lon"]))
    return None


def _categorise(tags: dict[str, str]) -> str | None:
    if tags.get("amenity") == "fuel":
        return "fuel"
    if tags.get("amenity") == "cafe":
        return "cafe"
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    return None


def _default_name(category: str) -> str:
    return {"fuel": "Fuel station", "cafe": "Cafe", "viewpoint": "Viewpoint"}[category]


def _describe(tags: dict[str, str], category: str) -> str:
    """A short line from the tags that actually change a decision."""
    if category == "fuel":
        interesting = ("brand", "operator", "opening_hours", "fuel:octane_95",
                       "fuel:diesel", "payment:cash", "self_service")
    elif category == "cafe":
        interesting = ("cuisine", "opening_hours", "outdoor_seating", "internet_access")
    else:
        interesting = ("ele", "direction", "description")
    parts = [f"{key.split(':')[-1]}: {tags[key]}" for key in interesting if tags.get(key)]
    return " · ".join(parts)[:250]
