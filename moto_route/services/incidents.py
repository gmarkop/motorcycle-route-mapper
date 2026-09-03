"""Live road-authority incidents, behind a provider interface.

This is the layer OpenStreetMap cannot give you: today's crash, this week's
lane closure, the diversion that went up on Tuesday. There is no single free
pan-European feed, so the design is a small interface and two implementations
rather than one hard-coded source:

:class:`GeoJsonFeedProvider`
    Bring your own URL. Many national and regional authorities publish
    incidents as GeoJSON, and this maps a standard ``FeatureCollection`` onto
    the app's model. Point and LineString geometries both work. Configure with
    ``MOTO_INCIDENT_FEEDS`` as a comma-separated list.

:class:`AutobahnProvider`
    Germany's Autobahn GmbH publishes roadworks, closures and warnings for the
    federal motorways with no key and no registration. Which motorways your
    route touches is worked out from OpenStreetMap, so it needs no manual setup.

**Verification status:** the Autobahn endpoints could not be reached from the
environment this was written in, so the response mapping is written to the
documented shape and covered by tests against recorded fixtures, but has not
been exercised against the live service. Treat it as best-effort until you have
seen it return real data; the layer fails soft, so a wrong guess about the
schema shows an empty layer rather than breaking the app.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence

import httpx

from .. import geo
from ..config import Settings
from ..models import Route
from .cache import TTLCache
from .overpass import OverpassError, query_header, run_query

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Incident:
    lat: float
    lon: float
    source: str
    category: str          # roadworks | closure | warning | incident
    title: str
    detail: str
    url: str = ""
    starts_at: str = ""
    distance_along_route_m: float = 0.0
    distance_off_route_m: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["distance_along_route_m"] = round(self.distance_along_route_m)
        data["distance_off_route_m"] = round(self.distance_off_route_m)
        return data


class IncidentProvider(Protocol):
    """What a source of live incidents has to offer.

    Deliberately narrow: given a route, return incidents anywhere near it. The
    caller handles corridor filtering, positioning along the route, sorting and
    error reporting, so a new national feed is one small class and nothing else.
    """

    name: str

    async def fetch(self, route: Route, client: httpx.AsyncClient) -> list[Incident]:
        ...

    def covers(self, route: Route) -> bool:
        """Whether this provider has anything to say about this route.

        Optional — a provider without one is assumed to cover everything. It
        exists because a national feed asked about another country is worse
        than useless: it spends a request, and its road numbering may collide
        with the local one. Greek motorways are numbered A1, A2, … exactly as
        German ones are.
        """


# --------------------------------------------------------------------- generic

class GeoJsonFeedProvider:
    """Read incidents from any GeoJSON ``FeatureCollection``.

    Property names differ between authorities, so several common spellings are
    tried for each field rather than demanding one schema.
    """

    def __init__(self, url: str, name: str = "") -> None:
        self.url = url
        self.name = name or _host_of(url)

    async def fetch(self, route: Route, client: httpx.AsyncClient) -> list[Incident]:
        response = await client.get(self.url)
        response.raise_for_status()
        payload = response.json()

        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            raise ValueError(f"{self.name}: expected a GeoJSON FeatureCollection")

        incidents = []
        for feature in features:
            incident = self._feature_to_incident(feature)
            if incident is not None:
                incidents.append(incident)
        return incidents

    def _feature_to_incident(self, feature: Any) -> Incident | None:
        if not isinstance(feature, dict):
            return None
        properties = feature.get("properties") or {}
        position = _geojson_position(feature.get("geometry") or {})
        if position is None:
            return None

        return Incident(
            lat=position[0],
            lon=position[1],
            source=self.name,
            category=_normalise_category(
                _first(properties, "type", "category", "eventType", "incidentType")),
            title=_first(properties, "title", "name", "headline", "description",
                         default="Incident"),
            detail=_first(properties, "description", "detail", "comment", "subtitle"),
            url=_first(properties, "url", "link", "href"),
            starts_at=_first(properties, "startTime", "start", "startTimestamp", "from"),
        )


# ---------------------------------------------------------------------- Germany

class AutobahnProvider:
    """Germany's Autobahn GmbH open API — no key, no registration.

    The API is organised per motorway (``A8``, ``A81``, …), so the first job is
    working out which ones the route actually uses. Asking Overpass for the
    motorway ``ref`` tags along the route is cheaper and far more accurate than
    querying all ~120 German motorways, and it means the layer needs no manual
    configuration for a route that happens to cross Germany.
    """

    name = "Autobahn (DE)"

    #: Germany, generously bounded. A route that never enters this box cannot
    #: use a German motorway, so asking is a wasted Overpass slot — and worse,
    #: Greek and Austrian motorway refs look identical to German ones, so the
    #: detection would happily return "A1" and query the wrong country's roads.
    GERMANY = (47.2, 5.8, 55.1, 15.1)   # min_lat, min_lon, max_lat, max_lon

    #: The three per-road services, and what each means for a rider.
    SERVICES = {
        "roadworks": "roadworks",
        "closure": "closure",
        "warning": "warning",
    }

    def __init__(self, settings: Settings, cache: TTLCache, roads: Sequence[str] | None = None) -> None:
        self.settings = settings
        self.cache = cache
        self._configured_roads = list(roads or settings.autobahn_roads)

    def covers(self, route: Route) -> bool:
        bounds = route.bounds()
        if bounds is None:
            return False
        min_lat, min_lon, max_lat, max_lon = bounds
        g_min_lat, g_min_lon, g_max_lat, g_max_lon = self.GERMANY
        # Box overlap, not containment: a ride from Munich to Salzburg is partly
        # in Germany and its Autobahn stretch still matters.
        return not (max_lat < g_min_lat or min_lat > g_max_lat
                    or max_lon < g_min_lon or min_lon > g_max_lon)

    async def fetch(self, route: Route, client: httpx.AsyncClient) -> list[Incident]:
        if not self._configured_roads and not self.covers(route):
            return []
        roads = self._configured_roads or await self._detect_roads(route, client)
        if not roads:
            return []

        # Cap the fan-out: a route crossing Germany end to end could otherwise
        # fire dozens of requests at a free service.
        roads = roads[: self.settings.max_autobahn_roads]

        tasks = [
            self._fetch_service(client, road, service, category)
            for road in roads
            for service, category in self.SERVICES.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        incidents: list[Incident] = []
        for result in results:
            if isinstance(result, Exception):
                log.debug("Autobahn sub-request failed: %s", result)
                continue
            incidents.extend(result)
        return incidents

    async def _fetch_service(
        self,
        client: httpx.AsyncClient,
        road: str,
        service: str,
        category: str,
    ) -> list[Incident]:
        url = f"{self.settings.autobahn_url.rstrip('/')}/{road}/services/{service}"
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()

        # The payload key matches the service name, e.g. {"roadworks": [...]}.
        items = payload.get(service) if isinstance(payload, dict) else None
        if not isinstance(items, list):
            # Some services use a plural the endpoint name does not predict;
            # fall back to the first list in the object rather than giving up.
            items = next((v for v in (payload or {}).values() if isinstance(v, list)), [])

        incidents = []
        for item in items:
            incident = self._item_to_incident(item, road, category)
            if incident is not None:
                incidents.append(incident)
        return incidents

    def _item_to_incident(self, item: Any, road: str, category: str) -> Incident | None:
        if not isinstance(item, dict):
            return None
        coordinate = item.get("coordinate") or {}
        lat = _to_float(coordinate.get("lat"))
        # The API spells longitude "long", not "lon" — an easy and silent trap.
        lon = _to_float(coordinate.get("long") or coordinate.get("lon"))
        if lat is None or lon is None:
            return None

        description = item.get("description")
        if isinstance(description, list):
            detail = " ".join(str(line) for line in description if line).strip()
        else:
            detail = str(description or "")

        return Incident(
            lat=lat,
            lon=lon,
            source=f"{self.name} {road}",
            category=category,
            title=str(item.get("title") or item.get("subtitle") or road).strip(),
            detail=detail[:400],
            starts_at=str(item.get("startTimestamp") or ""),
        )

    async def _detect_roads(self, route: Route, client: httpx.AsyncClient) -> list[str]:
        """Motorway refs along the route, from OpenStreetMap."""
        coords = geo.simplify(route.all_latlon, 1000.0)[:200]
        if len(coords) < 2:
            return []
        joined = ",".join(f"{lat:.4f},{lon:.4f}" for lat, lon in coords)
        query = (
            query_header(self.settings)
            + f'way(around:200,{joined})["highway"="motorway"]["ref"];'
            + "out tags 200;"
        )

        cached = self.cache.get("autobahn-roads|" + query)
        if cached is None:
            try:
                cached = await run_query(query, self.settings, client)
            except OverpassError as exc:
                log.info("Could not detect motorways for the Autobahn feed: %s", exc)
                return []
            # Motorway numbering does not change; cache it for a long time.
            self.cache.set("autobahn-roads|" + query, cached, self.settings.routing_ttl_s)

        roads: list[str] = []
        for element in cached.get("elements", []):
            ref = ((element.get("tags") or {}).get("ref") or "").strip()
            # German motorway refs look like "A 8" or "A8"; anything else is
            # another country's motorway and this provider does not cover it.
            compact = ref.replace(" ", "")
            if compact.startswith("A") and compact[1:].isdigit() and compact not in roads:
                roads.append(compact)
        return roads


# ------------------------------------------------------------------ the service

async def find_incidents(
    route: Route,
    settings: Settings,
    client: httpx.AsyncClient,
    cache: TTLCache,
) -> dict[str, Any]:
    """Query every configured provider and merge what comes back."""
    route_points = route.all_latlon
    if len(route_points) < 2:
        return {"available": False, "reason": "Route has no line to search along.",
                "incidents": []}
    if settings.offline:
        return {"available": False, "reason": "Offline mode is enabled.", "incidents": []}

    configured = build_providers(settings, cache)
    if not configured:
        return {
            "available": False,
            "reason": ("No incident feed configured. Set MOTO_INCIDENT_FEEDS to a "
                       "GeoJSON URL, or enable the German Autobahn provider."),
            "incidents": [],
        }

    providers = [p for p in configured if _covers(p, route)]
    if not providers:
        return {
            "available": False,
            "reason": ("No configured incident feed covers this route — they are "
                       f"for elsewhere ({', '.join(p.name for p in configured)}). "
                       "Add a feed for this country with MOTO_INCIDENT_FEEDS."),
            "incidents": [],
        }

    results = await asyncio.gather(
        *(provider.fetch(route, client) for provider in providers),
        return_exceptions=True,
    )

    incidents: list[Incident] = []
    failures: list[str] = []
    for provider, result in zip(providers, results):
        if isinstance(result, Exception):
            log.warning("Incident provider %s failed: %s", provider.name, result)
            failures.append(f"{provider.name} ({type(result).__name__})")
            continue
        incidents.extend(result)

    positioned = _filter_to_corridor(incidents, route_points, settings)

    return {
        "available": True,
        "incidents": [incident.to_dict() for incident in positioned],
        "sources": [provider.name for provider in providers],
        "failed_sources": failures,
        "note": (
            "Live incidents from road authorities. Coverage is only as good as "
            "the feeds you have configured — an empty layer means no feed "
            "covers this road, not that the road is clear."
        ),
    }


def _covers(provider: IncidentProvider, route: Route) -> bool:
    """A provider without a `covers` method is assumed to cover everywhere."""
    check = getattr(provider, "covers", None)
    return True if check is None else bool(check(route))


def build_providers(settings: Settings, cache: TTLCache) -> list[IncidentProvider]:
    providers: list[IncidentProvider] = []
    for url in settings.incident_feeds:
        if url:
            providers.append(GeoJsonFeedProvider(url))
    if settings.autobahn_enabled:
        providers.append(AutobahnProvider(settings, cache))
    return providers


def _filter_to_corridor(
    incidents: Sequence[Incident],
    route_points: Sequence[geo.LatLon],
    settings: Settings,
) -> list[Incident]:
    """Keep only what is near the route, and say where along it each one is.

    A national feed covers a whole country; without this every roadworks in
    Germany would land on the map.
    """
    cumulative = geo.cumulative_distances(route_points)
    kept: list[Incident] = []

    for incident in incidents:
        off_route, along_route = geo.project_onto_polyline(
            (incident.lat, incident.lon), route_points, cumulative)
        if off_route > settings.incident_corridor_m:
            continue
        incident.distance_off_route_m = off_route
        incident.distance_along_route_m = along_route
        kept.append(incident)

    kept.sort(key=lambda item: item.distance_along_route_m)
    return kept[: settings.max_incidents]


# ----------------------------------------------------------------- small helpers

def _geojson_position(geometry: dict[str, Any]) -> tuple[float, float] | None:
    """A representative (lat, lon) for a GeoJSON geometry.

    GeoJSON coordinates are ``[longitude, latitude]`` — the same order trap KML
    sets, and worth checking twice whenever a new feed looks offset.
    """
    coordinates = geometry.get("coordinates")
    kind = geometry.get("type")

    if kind == "Point" and isinstance(coordinates, list) and len(coordinates) >= 2:
        lon, lat = _to_float(coordinates[0]), _to_float(coordinates[1])
        return (lat, lon) if lat is not None and lon is not None else None

    if kind in {"LineString", "MultiPoint"} and isinstance(coordinates, list) and coordinates:
        # Use the midpoint: the start of a 20 km roadworks stretch may be well
        # outside the corridor while the works themselves are on the route.
        middle = coordinates[len(coordinates) // 2]
        if isinstance(middle, list) and len(middle) >= 2:
            lon, lat = _to_float(middle[0]), _to_float(middle[1])
            return (lat, lon) if lat is not None and lon is not None else None

    if kind in {"MultiLineString", "Polygon"} and isinstance(coordinates, list) and coordinates:
        return _geojson_position({"type": "LineString", "coordinates": coordinates[0]})

    return None


def _normalise_category(raw: str) -> str:
    lowered = (raw or "").lower()
    if any(word in lowered for word in ("closed", "closure", "sperrung")):
        return "closure"
    if any(word in lowered for word in ("roadwork", "construction", "baustelle", "works")):
        return "roadworks"
    if any(word in lowered for word in ("warn", "hazard", "danger")):
        return "warning"
    return "incident"


def _first(properties: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = properties.get(key)
        if value not in (None, "", []):
            return str(value)[:400]
    return default


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _host_of(url: str) -> str:
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0] or "feed"
