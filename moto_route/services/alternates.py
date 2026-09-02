"""Alternate ways round the same ride, via OSRM.

Two situations make this worth having on a tour:

* A pass is shut (see :mod:`.hazards`) and you need the next way over.
* The forecast says the eastern valley is being rained on and the western one
  is not.

OSRM's public demo server answers the routing; what this module adds is the
comparison a rider actually wants. Each candidate is measured for *curviness*
as well as distance and time, because "12 minutes longer but twice the corners"
is usually the right trade on a motorcycle and no car navigation app will ever
offer it to you.

Note the OSRM constraint: alternatives are only produced for a plain A-to-B
request. Pin down the middle with via points and you get exactly one route back,
which is why the origin and destination of the loaded route are used here.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .. import geo
from ..config import Settings
from ..models import Route
from .cache import TTLCache

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Alternate:
    label: str
    distance_m: float
    duration_s: float
    curviness: float
    coordinates: list[list[float]]  # [[lat, lon], ...] ready for Leaflet
    is_original: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["distance_km"] = round(self.distance_m / 1000.0, 1)
        data["duration_min"] = round(self.duration_s / 60.0)
        data["curviness"] = round(self.curviness, 1)
        return data


async def find_alternates(
    route: Route,
    settings: Settings,
    client: httpx.AsyncClient,
    cache: TTLCache,
) -> dict[str, Any]:
    points = route.all_latlon
    if len(points) < 2:
        return {"available": False, "reason": "Route has no start and end.", "alternates": []}
    if settings.offline:
        return {"available": False, "reason": "Offline mode is enabled.", "alternates": []}

    start, end = points[0], points[-1]
    if geo.haversine_m(start, end) < 500:
        # A loop starts and ends in the same place, so "route me from A to A"
        # is meaningless. Say so rather than returning a zero-length route.
        return {
            "available": False,
            "reason": "This is a loop — alternates need a different start and finish.",
            "alternates": [],
        }

    url = (
        f"{settings.osrm_url.rstrip('/')}/route/v1/driving/"
        f"{start[1]:.5f},{start[0]:.5f};{end[1]:.5f},{end[0]:.5f}"
    )
    params = {
        "alternatives": "3",
        "overview": "simplified",
        "geometries": "geojson",
        "steps": "false",
    }
    cache_key = f"osrm|{url}|{sorted(params.items())}"

    cached = cache.get(cache_key)
    if cached is None:
        try:
            response = await client.get(
                url, params=params, headers={"User-Agent": settings.user_agent}
            )
            response.raise_for_status()
            cached = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("OSRM request failed: %s", exc)
            stale = cache.get_stale(cache_key)
            if stale is None:
                return {
                    "available": False,
                    "reason": f"Routing service unavailable ({type(exc).__name__}).",
                    "alternates": [],
                }
            cached = stale
        else:
            cache.set(cache_key, cached, settings.routing_ttl_s)

    if cached.get("code") != "Ok":
        return {
            "available": False,
            "reason": f"Router could not connect these points ({cached.get('code', 'unknown')}).",
            "alternates": [],
        }

    alternates = _parse_routes(cached.get("routes", []))
    original = _describe_original(route)

    return {
        "available": True,
        "original": original.to_dict(),
        "alternates": [a.to_dict() for a in alternates],
        "note": (
            "Alternates connect the start and finish of your file directly, so "
            "they ignore the via points in between."
        ),
    }


def _parse_routes(raw_routes: list[dict[str, Any]]) -> list[Alternate]:
    alternates: list[Alternate] = []

    for index, raw in enumerate(raw_routes):
        # GeoJSON is [longitude, latitude]; Leaflet wants [latitude, longitude].
        coordinates = [
            [float(lat), float(lon)]
            for lon, lat in (raw.get("geometry") or {}).get("coordinates", [])
        ]
        if len(coordinates) < 2:
            continue
        alternates.append(
            Alternate(
                label=f"Option {index + 1}",
                distance_m=float(raw.get("distance", 0.0)),
                duration_s=float(raw.get("duration", 0.0)),
                curviness=geo.curviness_deg_per_km([(lat, lon) for lat, lon in coordinates]),
                coordinates=coordinates,
            )
        )

    if not alternates:
        return []

    # Name them by what makes each one worth choosing.
    fastest = min(alternates, key=lambda a: a.duration_s)
    fastest.label = "Fastest"
    twistiest = max(alternates, key=lambda a: a.curviness)
    if twistiest is not fastest:
        twistiest.label = "Most corners"
    shortest = min(alternates, key=lambda a: a.distance_m)
    if shortest is not fastest and shortest is not twistiest:
        shortest.label = "Shortest"

    return alternates


def _describe_original(route: Route) -> Alternate:
    """Measure the loaded route the same way, so the comparison is like for like."""
    points = route.all_latlon
    return Alternate(
        label="Your route",
        distance_m=route.distance_m,
        duration_s=0.0,  # unknown: the file rarely carries a realistic time
        curviness=geo.curviness_deg_per_km(points),
        coordinates=[],  # already drawn on the map; no need to send it twice
        is_original=True,
    )
