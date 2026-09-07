"""Runtime settings.

Everything is read from environment variables with sensible defaults, so the
app runs with no configuration at all but can be pointed at your own API
endpoints (a self-hosted OSRM or Overpass instance, say) without touching code.

All three data sources are free and need no API key:

* **Open-Meteo** for weather — free for non-commercial use, no registration.
* **Overpass** for OpenStreetMap road closures and construction.
* **OSRM** for alternate routes — the public demo server is rate-limited and
  explicitly not for production, so heavy users should self-host it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bbox(name: str) -> tuple[float, float, float, float] | None:
    """A "south,west,north,east" environment value, or None when unset.

    A malformed value is refused rather than ignored. This one decides whether
    a self-hosted Overpass is trusted to answer for a given route, and a typo
    that silently disabled the check would reinstate exactly the failure the
    setting exists to prevent.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(f"{name} must be 'south,west,north,east', got {raw!r}")
    try:
        south, west, north, east = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"{name} must be four numbers, got {raw!r}") from exc
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError(
            f"{name} must be south,west,north,east with south<north and "
            f"west<east, got {raw!r}")
    return south, west, north, east


def _env_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    """Comma-separated environment value, e.g. MOTO_AUTOBAHN_ROADS=A8,A81.

    An unset variable falls back to the default; one set to an empty string
    means an empty list. Collapsing the two would make a default impossible to
    turn off, which matters for settings whose default is not empty.
    """
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    stripped = raw.strip()
    if not stripped:
        return []
    return [part.strip() for part in stripped.split(",") if part.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    # --- external services ---------------------------------------------------
    weather_url: str = field(
        default_factory=lambda: _env_str("MOTO_WEATHER_URL", "https://api.open-meteo.com/v1/forecast")
    )
    overpass_url: str = field(
        default_factory=lambda: _env_str("MOTO_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
    )
    osrm_url: str = field(
        default_factory=lambda: _env_str("MOTO_OSRM_URL", "https://router.project-osrm.org")
    )
    #: Turn every network call off. The map, the route and the profile still
    #: work; live layers report themselves as unavailable instead of hanging.
    offline: bool = field(default_factory=lambda: _env_bool("MOTO_OFFLINE", False))
    request_timeout_s: float = field(default_factory=lambda: _env_float("MOTO_TIMEOUT", 20.0))
    #: How many Overpass queries may be in flight at once. The public instance
    #: grants about two slots per IP, so two is the documented allowance and
    #: not a gamble. It was 1 while a 429 was an unhandled crash; now that 429
    #: is retried with backoff and rotated onto a mirror, queueing every query
    #: behind every other one costs more than it saves — the closure and POI
    #: layers are independent requests and serialising them doubles the wait
    #: the rider sees. Drop it back to 1 if you start seeing rate-limiting.
    overpass_concurrency: int = field(default_factory=lambda: _env_int("MOTO_OVERPASS_CONCURRENCY", 2))
    #: How long Overpass may spend on one query. This is declared inside the
    #: query AND used as the HTTP timeout, because the two must agree: telling
    #: Overpass it may take 90 seconds while hanging up after 20 guarantees a
    #: timeout on any query that is actually slow.
    overpass_timeout_s: int = field(default_factory=lambda: _env_int("MOTO_OVERPASS_TIMEOUT", 90))
    #: Coordinates per Overpass query. Cost grows with the length of the
    #: `around:` corridor, so a long route is split into several cheap queries
    #: rather than one that the public servers refuse. 44 points took ~15s on
    #: overpass-api.de; 169 in one query timed out.
    overpass_max_points: int = field(default_factory=lambda: _env_int("MOTO_OVERPASS_MAX_POINTS", 60))
    #: How the closure query is shaped. "filtered" applies each tag filter
    #: inside its own `around`, so Overpass uses the tag index and the result
    #: sets stay small. "grouped" walks the corridor once into a named set and
    #: filters that — fewer spatial passes, but it materialises every road in
    #: the corridor first, which through a town is thousands of ways and times
    #: out. Measured: grouped failed a chunk that filtered handles.
    overpass_query_style: str = field(
        default_factory=lambda: _env_str("MOTO_OVERPASS_QUERY_STYLE", "filtered"))
    #: Total seconds one layer may spend on Overpass, across all its chunks and
    #: retries. Without it a single stubborn chunk burned 311s — three attempts
    #: at the full per-request timeout — while the rider watched an empty panel.
    overpass_deadline_s: float = field(
        default_factory=lambda: _env_float("MOTO_OVERPASS_DEADLINE", 120.0))
    #: Other public Overpass instances to fall back to. The main server drops
    #: connections when it is busy, and a refused connection is precisely the
    #: failure a second endpoint fixes. Comma-separated; set empty to disable.
    #: Bounding box the primary Overpass instance actually holds, as
    #: "south,west,north,east". Set this when the primary is your own server
    #: built from country extracts: outside the box it would answer "nothing
    #: here" with total confidence, so outside the box it is not asked.
    #: Unset (the default) means the primary is assumed to cover everything,
    #: which is true of the public servers.
    overpass_coverage: tuple[float, float, float, float] | None = field(
        default_factory=lambda: _env_bbox("MOTO_OVERPASS_COVERAGE"))
    overpass_fallback_urls: list[str] = field(default_factory=lambda: _env_list(
        "MOTO_OVERPASS_FALLBACK_URLS",
        ("https://overpass.kumi.systems/api/interpreter",),
    ))

    # --- caching -------------------------------------------------------------
    cache_dir: Path = field(
        default_factory=lambda: Path(_env_str("MOTO_CACHE_DIR", str(Path.home() / ".cache" / "moto-route")))
    )
    weather_ttl_s: int = field(default_factory=lambda: _env_int("MOTO_WEATHER_TTL", 900))       # 15 min
    hazard_ttl_s: int = field(default_factory=lambda: _env_int("MOTO_HAZARD_TTL", 6 * 3600))    # 6 h
    #: TTL for an answer that is missing sections, kept far shorter than the
    #: complete one. The panel tells the rider to press Refresh to try the
    #: rest, and caching a partial result for six hours would make that a lie:
    #: every Refresh would replay the same gaps from the cache without ever
    #: asking Overpass again.
    partial_ttl_s: int = field(default_factory=lambda: _env_int("MOTO_PARTIAL_TTL", 300))   # 5 min
    routing_ttl_s: int = field(default_factory=lambda: _env_int("MOTO_ROUTING_TTL", 24 * 3600))

    # --- route handling ------------------------------------------------------
    max_upload_bytes: int = field(default_factory=lambda: _env_int("MOTO_MAX_UPLOAD", 25 * 1024 * 1024))
    #: Douglas-Peucker tolerance for the line sent to the browser. 15 m keeps
    #: hairpins visually intact while shedding most redundant track points.
    simplify_tolerance_m: float = field(default_factory=lambda: _env_float("MOTO_SIMPLIFY_M", 15.0))

    # --- weather along the route --------------------------------------------
    default_speed_kmh: float = field(default_factory=lambda: _env_float("MOTO_SPEED_KMH", 65.0))
    weather_interval_m: float = field(default_factory=lambda: _env_float("MOTO_WEATHER_INTERVAL_M", 25_000.0))
    max_weather_samples: int = field(default_factory=lambda: _env_int("MOTO_MAX_WEATHER_SAMPLES", 24))

    # --- points of interest --------------------------------------------------
    #: Fuel is worth a detour, so it gets a wider corridor than a cafe you would
    #: only stop at if it were on the way.
    fuel_corridor_m: float = field(default_factory=lambda: _env_float("MOTO_FUEL_CORRIDOR_M", 1000.0))
    cafe_corridor_m: float = field(default_factory=lambda: _env_float("MOTO_CAFE_CORRIDOR_M", 300.0))
    viewpoint_corridor_m: float = field(default_factory=lambda: _env_float("MOTO_VIEWPOINT_CORRIDOR_M", 500.0))
    max_pois: int = field(default_factory=lambda: _env_int("MOTO_MAX_POIS", 300))

    #: Usable tank range in km. Bikes carry far less fuel than cars, which is
    #: why this app plans around it and a car navigation app does not.
    tank_range_km: float = field(default_factory=lambda: _env_float("MOTO_TANK_RANGE_KM", 250.0))
    #: Fraction of the tank held back as reserve, so the plan never has you
    #: arriving at a pump on fumes.
    fuel_reserve_fraction: float = field(default_factory=lambda: _env_float("MOTO_FUEL_RESERVE", 0.15))

    # --- live incident feeds -------------------------------------------------
    #: GeoJSON incident feeds to merge in. Bring your own national or regional
    #: authority URL; see services/incidents.py for the shape expected.
    incident_feeds: list[str] = field(default_factory=lambda: _env_list("MOTO_INCIDENT_FEEDS"))
    #: Germany's Autobahn GmbH open API — keyless, so it is on by default.
    autobahn_enabled: bool = field(default_factory=lambda: _env_bool("MOTO_AUTOBAHN", True))
    autobahn_url: str = field(
        default_factory=lambda: _env_str("MOTO_AUTOBAHN_URL", "https://verkehr.autobahn.de/o/autobahn")
    )
    #: Pin the motorways to query. Empty means "work it out from the route".
    autobahn_roads: list[str] = field(default_factory=lambda: _env_list("MOTO_AUTOBAHN_ROADS"))
    #: Each road costs three requests, so cap the fan-out at a free service.
    max_autobahn_roads: int = field(default_factory=lambda: _env_int("MOTO_MAX_AUTOBAHN_ROADS", 8))
    incident_corridor_m: float = field(default_factory=lambda: _env_float("MOTO_INCIDENT_CORRIDOR_M", 500.0))
    max_incidents: int = field(default_factory=lambda: _env_int("MOTO_MAX_INCIDENTS", 150))
    incident_ttl_s: int = field(default_factory=lambda: _env_int("MOTO_INCIDENT_TTL", 600))  # 10 min

    # --- offline tiles -------------------------------------------------------
    #: OpenStreetMap's tile usage policy forbids bulk downloading and names 250
    #: tiles as the limit for an area. Staying under it by default keeps this a
    #: good citizen; raise it only when pointing at your own tile server.
    max_cached_tiles: int = field(default_factory=lambda: _env_int("MOTO_MAX_CACHED_TILES", 250))
    #: Milliseconds between prefetch requests, so caching never bursts.
    tile_prefetch_delay_ms: int = field(default_factory=lambda: _env_int("MOTO_TILE_DELAY_MS", 120))

    # --- hazards -------------------------------------------------------------
    #: How far from the route a closure may be and still count as "on my way".
    hazard_corridor_m: float = field(default_factory=lambda: _env_float("MOTO_HAZARD_CORRIDOR_M", 150.0))
    max_hazards: int = field(default_factory=lambda: _env_int("MOTO_MAX_HAZARDS", 200))

    @property
    def overpass_endpoints(self) -> list[str]:
        """Every Overpass instance to try, primary first, without duplicates."""
        endpoints = [self.overpass_url]
        for url in self.overpass_fallback_urls:
            if url and url not in endpoints:
                endpoints.append(url)
        return endpoints

    def endpoints_for(self, bounds: tuple[float, float, float, float] | None
                      ) -> list[str]:
        """The Overpass instances fit to answer about this piece of the world.

        A self-hosted instance built from country extracts holds only those
        countries, and it does not know that. Asked about a road outside them
        it returns an empty result and HTTP 200 — indistinguishable from a
        genuinely clear road, which is the worst possible answer: the rider is
        told there are no closures rather than told nothing is known.

        So when ``overpass_coverage`` is set, the primary is used only for a
        route that lies wholly inside it, and anything crossing the edge goes
        to the public servers instead. Without the setting nothing changes.

        ``bounds`` is (south, west, north, east), or None when the caller does
        not know, which is treated as "not provably inside".
        """
        endpoints = self.overpass_endpoints
        covers = self.overpass_coverage
        if not covers or len(endpoints) == 1:
            return endpoints

        south, west, north, east = covers
        if bounds is not None:
            b_south, b_west, b_north, b_east = bounds
            if (south <= b_south and b_north <= north
                    and west <= b_west and b_east <= east):
                return endpoints

        # Outside, straddling, or unknown: skip the local instance entirely
        # rather than let it answer for ground it has never seen.
        return endpoints[1:]

    @property
    def user_agent(self) -> str:
        # Overpass and OSRM ask that clients identify themselves; an anonymous
        # scraper is the first thing they rate-limit.
        return "moto-route-mapper/0.1 (personal motorcycle route planner)"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings, built once on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Drop the cached settings — used by tests that patch the environment."""
    global _settings
    _settings = None
