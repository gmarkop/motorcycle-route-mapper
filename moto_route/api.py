"""HTTP API and static file serving.

The design is deliberately server-light: the browser uploads a file once, the
server parses it, keeps it in memory under an id, and then answers three
independent enrichment endpoints for that id. Each live layer loads on its own,
so a slow Overpass query never delays the map, and a failing service degrades to
a message in the sidebar instead of an empty screen.

State lives in memory only. This is a personal tool you run on your own laptop,
where a restart losing the loaded route costs one drag-and-drop — worth it to
avoid a database.
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, export, geo
from .config import Settings, get_settings
from .models import Route
from .parsers import RouteParseError, SUPPORTED_EXTENSIONS, parse_route_bytes
from .services import alternates as alternates_service
from .services import elevation as elevation_service
from .services import hazards as hazards_service
from .services import incidents as incidents_service
from .services import pois as pois_service
from .services import weather as weather_service
from .services.cache import TTLCache

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: How many uploaded routes to keep. Riders open a handful of files in a
#: session; an unbounded dict would be a slow memory leak.
MAX_ROUTES_IN_MEMORY = 20


class RouteStore:
    """A tiny bounded LRU of parsed routes."""

    def __init__(self, capacity: int = MAX_ROUTES_IN_MEMORY) -> None:
        self._routes: OrderedDict[str, Route] = OrderedDict()
        self._capacity = capacity

    def add(self, route: Route) -> str:
        route_id = uuid.uuid4().hex[:12]
        self._routes[route_id] = route
        while len(self._routes) > self._capacity:
            self._routes.popitem(last=False)
        return route_id

    def get(self, route_id: str) -> Route:
        route = self._routes.get(route_id)
        if route is None:
            raise HTTPException(
                status_code=404,
                detail="That route is no longer loaded — upload the file again.",
            )
        self._routes.move_to_end(route_id)
        return route


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = RouteStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One client for the whole process: connection reuse matters when three
        # layers fire at once, and it keeps timeouts in a single place.
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            app.state.http = client
            yield

    app = FastAPI(title="Motorcycle Route Mapper", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.weather_cache = TTLCache(settings.cache_dir, "weather")
    app.state.hazard_cache = TTLCache(settings.cache_dir, "hazards")
    app.state.routing_cache = TTLCache(settings.cache_dir, "routing")
    app.state.poi_cache = TTLCache(settings.cache_dir, "pois")
    app.state.incident_cache = TTLCache(settings.cache_dir, "incidents")
    app.state.elevation_cache = TTLCache(settings.cache_dir, "elevation")

    # ------------------------------------------------------------------ routes

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "offline": settings.offline,
            "supported_formats": list(SUPPORTED_EXTENSIONS),
            # The frontend reads its defaults from here rather than hard-coding
            # them, so changing an environment variable moves both halves.
            "defaults": {
                "speed_kmh": settings.default_speed_kmh,
                "tank_range_km": settings.tank_range_km,
                "max_cached_tiles": settings.max_cached_tiles,
                "tile_prefetch_delay_ms": settings.tile_prefetch_delay_ms,
            },
            "incidents_configured": bool(settings.incident_feeds) or settings.autobahn_enabled,
        }

    @app.post("/api/routes")
    async def upload_route(file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read()
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {settings.max_upload_bytes // (1024*1024)} MB limit.",
            )
        try:
            route = parse_route_bytes(data, file.filename or "")
        except RouteParseError as exc:
            # A bad file is the user's most likely mistake, so the message has to
            # be readable rather than a stack trace in the console.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        route_id = store.add(route)
        log.info("Loaded %s: %d points, %.1f km", route.name, route.point_count,
                 route.distance_m / 1000)
        return {
            "id": route_id,
            "route": route.to_dict(simplify_m=settings.simplify_tolerance_m),
        }

    @app.get("/api/routes/{route_id}/weather")
    async def route_weather(
        route_id: str,
        departure: str | None = Query(None, description="ISO-8601 departure time; defaults to now"),
        speed_kmh: float = Query(0, ge=0, le=200, description="Average moving speed"),
    ) -> dict[str, Any]:
        route = store.get(route_id)
        return await weather_service.forecast_along_route(
            route,
            departure=_parse_departure(departure),
            speed_kmh=speed_kmh or settings.default_speed_kmh,
            settings=settings,
            client=app.state.http,
            cache=app.state.weather_cache,
        )

    @app.get("/api/routes/{route_id}/hazards")
    async def route_hazards(route_id: str) -> dict[str, Any]:
        route = store.get(route_id)
        return await hazards_service.find_hazards(
            route, settings, app.state.http, app.state.hazard_cache
        )

    @app.get("/api/routes/{route_id}/alternates")
    async def route_alternates(route_id: str) -> dict[str, Any]:
        route = store.get(route_id)
        return await alternates_service.find_alternates(
            route, settings, app.state.http, app.state.routing_cache
        )

    @app.get("/api/routes/{route_id}/pois")
    async def route_pois(
        route_id: str,
        tank_range_km: float = Query(0, ge=0, le=1000,
                                     description="Usable tank range; 0 uses the configured default"),
    ) -> dict[str, Any]:
        route = store.get(route_id)
        return await pois_service.find_pois(
            route, settings, app.state.http, app.state.poi_cache,
            tank_range_km=tank_range_km or None,
        )

    @app.get("/api/routes/{route_id}/incidents")
    async def route_incidents(route_id: str) -> dict[str, Any]:
        route = store.get(route_id)
        return await incidents_service.find_incidents(
            route, settings, app.state.http, app.state.incident_cache
        )

    @app.get("/api/routes/{route_id}/curviness")
    async def route_curviness(
        route_id: str,
        window_m: float = Query(600, ge=100, le=5000,
                                description="Sliding window used to average heading change"),
    ) -> dict[str, Any]:
        """Curviness sampled along the route, for the heat map.

        Returns the coordinates alongside the values so the frontend can draw
        coloured segments without re-deriving which point is which.
        """
        route = store.get(route_id)
        points = route.all_latlon
        profile = geo.curviness_profile(points, window_m=window_m)
        if not profile:
            return {"available": False, "reason": "Route is too short to measure.",
                    "samples": []}

        # Gradient turns a corner into a different corner. A 180 on the flat and
        # the same 180 on a 9% descent are not the same piece of riding, and
        # curvature alone cannot tell them apart.
        heights = await elevation_service.profile(
            points, [p.ele for p in route.iter_points()], settings,
            app.state.http, app.state.elevation_cache)
        slopes = elevation_service.gradient_at(
            heights.get("samples", []), [distance for _, distance, _ in profile])

        samples = [
            {
                "lat": round(points[index][0], 6),
                "lon": round(points[index][1], 6),
                "distance_m": round(distance),
                "curviness": round(value, 1),
                "gradient_pct": slope,
            }
            for (index, distance, value), slope in zip(profile, slopes)
        ]

        return {
            "available": True,
            "overall": round(geo.curviness_deg_per_km(points), 1),
            "window_m": window_m,
            "elevation": {"available": bool(heights.get("available")),
                          "source": heights.get("source"),
                          "reason": heights.get("reason")},
            "demanding": (elevation_service.demanding_stretches(samples, settings)
                          if heights.get("available") else []),
            "samples": samples,
        }

    @app.get("/api/routes/{route_id}/export.gpx")
    async def export_route(
        route_id: str,
        departure: str | None = Query(None),
        speed_kmh: float = Query(0, ge=0, le=200),
        tank_range_km: float = Query(0, ge=0, le=1000),
        include_shaping: bool = Query(False),
    ) -> Response:
        """Write the route back out as GPX with the live findings folded in.

        The enrichment layers are re-requested here, but they answer from cache
        after the sidebar has already loaded them, so exporting costs nothing
        extra in practice.
        """
        route = store.get(route_id)

        weather_data = await weather_service.forecast_along_route(
            route,
            departure=_parse_departure(departure),
            speed_kmh=speed_kmh or settings.default_speed_kmh,
            settings=settings,
            client=app.state.http,
            cache=app.state.weather_cache,
        )
        hazard_data = await hazards_service.find_hazards(
            route, settings, app.state.http, app.state.hazard_cache
        )
        poi_data = await pois_service.find_pois(
            route, settings, app.state.http, app.state.poi_cache,
            tank_range_km=tank_range_km or None,
        )

        payload = export.build_gpx(
            route, weather_data, hazard_data, poi_data,
            include_shaping_points=include_shaping,
        )
        filename = export.suggested_filename(route)
        return Response(
            content=payload,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/routes/{route_id}/elevation")
    async def route_elevation(route_id: str) -> dict[str, Any]:
        """Distance, elevation and gradient along the route.

        Served separately from the route because a long track makes a big
        payload. Heights come from the file when it has them and from a terrain
        model when it does not — a converted Google Maps route carries none,
        and used to get an empty panel here.
        """
        route = store.get(route_id)
        points = list(route.iter_points())
        coords = [p.as_latlon() for p in points]
        return await elevation_service.profile(
            coords, [p.ele for p in points], settings,
            app.state.http, app.state.elevation_cache)

    # ----------------------------------------------------------------- statics

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/sw.js")
        async def service_worker() -> FileResponse:
            """Serve the service worker from the site root.

            Scope is derived from the worker's own path: one served from
            /static/ can only ever control /static/, so it would never see the
            page or its tile requests. Serving the same file from / is the
            standard fix, and avoids having to set Service-Worker-Allowed.
            """
            return FileResponse(
                STATIC_DIR / "sw.js",
                media_type="application/javascript",
                headers={"Cache-Control": "no-cache"},
            )

    @app.exception_handler(RouteParseError)
    async def parse_error_handler(_request, exc: RouteParseError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


def _parse_departure(raw: str | None) -> datetime:
    """Accept an ISO timestamp, defaulting to now, always timezone-aware.

    A naive timestamp from a browser's ``datetime-local`` input is read as UTC;
    the frontend sends an explicit offset, so this is only a safety net.
    """
    if not raw:
        return datetime.now(timezone.utc)
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Bad departure time: {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


app = create_app()
