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

    # --- caching -------------------------------------------------------------
    cache_dir: Path = field(
        default_factory=lambda: Path(_env_str("MOTO_CACHE_DIR", str(Path.home() / ".cache" / "moto-route")))
    )
    weather_ttl_s: int = field(default_factory=lambda: _env_int("MOTO_WEATHER_TTL", 900))       # 15 min
    hazard_ttl_s: int = field(default_factory=lambda: _env_int("MOTO_HAZARD_TTL", 6 * 3600))    # 6 h
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

    # --- hazards -------------------------------------------------------------
    #: How far from the route a closure may be and still count as "on my way".
    hazard_corridor_m: float = field(default_factory=lambda: _env_float("MOTO_HAZARD_CORRIDOR_M", 150.0))
    max_hazards: int = field(default_factory=lambda: _env_int("MOTO_MAX_HAZARDS", 200))

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
