"""Weather *along* the route, not just at its start.

The useful question on a tour is never "what is the weather in Munich" but
"what will it be doing where I actually am, when I get there". So the route is
sampled every ~25 km, each sample gets an estimated time of arrival from your
departure time and average speed, and the forecast is read at that hour.

Data comes from Open-Meteo: free, no API key, and it accepts many coordinates
in a single request — which is what makes a 20-sample forecast one HTTP call
instead of twenty.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import httpx

from .. import geo
from ..config import Settings
from ..models import Route
from .cache import TTLCache

log = logging.getLogger(__name__)

#: WMO weather interpretation codes, as returned by Open-Meteo.
WMO_DESCRIPTIONS: dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snowfall", 73: "Moderate snowfall", 75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}

_FREEZING_CODES = {56, 57, 66, 67, 71, 73, 75, 77, 85, 86}
_THUNDER_CODES = {95, 96, 99}
_FOG_CODES = {45, 48}

_HOURLY_VARIABLES = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
    "visibility",
)


@dataclass(slots=True)
class WeatherPoint:
    """The forecast at one sample point, at the hour you are expected there."""

    lat: float
    lon: float
    distance_m: float
    eta: str
    forecast_hour: str = ""
    temperature_c: float | None = None
    apparent_c: float | None = None
    precipitation_mm: float | None = None
    precipitation_probability: int | None = None
    wind_kmh: float | None = None
    gust_kmh: float | None = None
    visibility_m: float | None = None
    weather_code: int | None = None
    description: str = ""
    rideability: int = 100
    warnings: list[str] = field(default_factory=list)
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rideability(
    *,
    temperature_c: float | None,
    precipitation_mm: float | None,
    precipitation_probability: int | None,
    gust_kmh: float | None,
    visibility_m: float | None,
    weather_code: int | None,
) -> tuple[int, list[str]]:
    """Score conditions from 0 (stay home) to 100 (perfect), with reasons.

    This is a deliberately opinionated heuristic tuned for two wheels, not a
    meteorological product. The weighting reflects what actually ends a ride:
    ice first, then thunderstorms and gusts, then cold, then rain. A car driver
    would weight almost none of this the same way.

    Every penalty that fires also produces a human-readable warning, because a
    bare "62/100" tells you nothing about whether to pack the rain suit or
    cancel.
    """
    score = 100.0
    warnings: list[str] = []

    # Ice is categorically different from bad weather: it is the one condition
    # where the correct answer is usually "do not ride".
    if temperature_c is not None and temperature_c <= 3.0:
        if (precipitation_mm or 0) > 0.05 or (weather_code in _FREEZING_CODES):
            score -= 55
            warnings.append("Freezing precipitation risk — black ice possible")
        else:
            score -= 15
            warnings.append(f"Near freezing ({temperature_c:.0f} °C)")
    elif weather_code in _FREEZING_CODES:
        score -= 40
        warnings.append("Snow or freezing rain forecast")

    if weather_code in _THUNDER_CODES:
        score -= 30
        warnings.append("Thunderstorms forecast")

    if weather_code in _FOG_CODES or (visibility_m is not None and visibility_m < 1000):
        score -= 20
        warnings.append("Poor visibility (fog)")

    if precipitation_mm is not None and precipitation_mm > 0.1:
        # Roughly: drizzle is a nuisance, 4 mm/h is a soaking.
        score -= min(30.0, precipitation_mm * 8.0)
        if precipitation_mm >= 2.0:
            warnings.append(f"Heavy rain ({precipitation_mm:.1f} mm/h)")
        elif precipitation_mm >= 0.5:
            warnings.append(f"Rain ({precipitation_mm:.1f} mm/h)")

    if precipitation_probability is not None and precipitation_probability >= 40:
        score -= min(20.0, (precipitation_probability - 40) * 0.33)
        if precipitation_probability >= 70 and not any("ain" in w for w in warnings):
            warnings.append(f"{precipitation_probability}% chance of rain")

    # Gusts, not average wind, are what move a bike across its lane.
    if gust_kmh is not None and gust_kmh > 35:
        score -= min(30.0, (gust_kmh - 35) * 1.2)
        if gust_kmh >= 55:
            warnings.append(f"Strong gusts ({gust_kmh:.0f} km/h) — crosswind risk")
        elif gust_kmh >= 45:
            warnings.append(f"Gusty ({gust_kmh:.0f} km/h)")

    if temperature_c is not None:
        if 3.0 < temperature_c < 10.0:
            score -= (10.0 - temperature_c) * 2.0
            warnings.append(f"Cold ({temperature_c:.0f} °C) — heated grips weather")
        elif temperature_c > 33.0:
            score -= min(20.0, (temperature_c - 33.0) * 2.5)
            warnings.append(f"Heat ({temperature_c:.0f} °C) — hydrate, watch for fatigue")

    return int(max(0.0, min(100.0, round(score)))), warnings


def plan_samples(
    route: Route,
    departure: datetime,
    speed_kmh: float,
    settings: Settings,
) -> list[tuple[float, float, float, datetime]]:
    """Pick where to ask for weather, and work out when you will be there.

    Returns ``(lat, lon, distance_m, eta)`` tuples. The ETA model is
    intentionally simple — constant average speed — because the honest
    alternative needs live traffic, and the hour-resolution forecast cannot tell
    the difference anyway.
    """
    points = route.all_latlon
    if not points:
        return []

    speed = max(speed_kmh, 5.0)
    samples = geo.sample_every(
        points,
        interval_m=settings.weather_interval_m,
        max_samples=settings.max_weather_samples,
    )

    planned = []
    for index, distance_m in samples:
        hours = (distance_m / 1000.0) / speed
        eta = departure + timedelta(hours=hours)
        lat, lon = points[index]
        planned.append((lat, lon, distance_m, eta))
    return planned


async def forecast_along_route(
    route: Route,
    departure: datetime,
    speed_kmh: float,
    settings: Settings,
    client: httpx.AsyncClient,
    cache: TTLCache,
) -> dict[str, Any]:
    """Fetch and score the forecast for every sample point on the route."""
    planned = plan_samples(route, departure, speed_kmh, settings)
    if not planned:
        return {"available": False, "reason": "Route has no points.", "points": []}
    if settings.offline:
        return {"available": False, "reason": "Offline mode is enabled.", "points": []}

    cache_key = _cache_key(planned)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        raw = await _fetch(planned, settings, client)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Weather request failed: %s", exc)
        stale = cache.get_stale(cache_key)
        if stale is not None:
            stale = dict(stale, stale=True, reason="Showing the last forecast — live update failed.")
            return stale
        return {
            "available": False,
            "reason": f"Weather service unreachable ({type(exc).__name__}).",
            "points": [],
        }

    points = [
        _build_point(lat, lon, distance_m, eta, series)
        for (lat, lon, distance_m, eta), series in zip(planned, raw)
    ]
    result = {"available": True, "stale": False, "points": [p.to_dict() for p in points],
              "summary": summarise(points)}
    cache.set(cache_key, result, settings.weather_ttl_s)
    return result


def summarise(points: Sequence[WeatherPoint]) -> dict[str, Any]:
    """Roll the per-point scores into the one line you read before leaving."""
    if not points:
        return {}

    scored = [p for p in points if p.temperature_c is not None]
    if not scored:
        return {}

    worst = min(scored, key=lambda p: p.rideability)
    temps = [p.temperature_c for p in scored if p.temperature_c is not None]
    # Preserve first-seen order while removing duplicate warning text.
    warnings = list(dict.fromkeys(w for p in scored for w in p.warnings))

    return {
        "worst_rideability": worst.rideability,
        "worst_at_km": round(worst.distance_m / 1000.0, 1),
        "worst_description": worst.description,
        "average_rideability": int(round(sum(p.rideability for p in scored) / len(scored))),
        "min_temperature_c": round(min(temps), 1),
        "max_temperature_c": round(max(temps), 1),
        "warnings": warnings[:6],
        "verdict": _verdict(min(p.rideability for p in scored)),
    }


def _verdict(worst_score: int) -> str:
    if worst_score >= 80:
        return "Good riding conditions."
    if worst_score >= 60:
        return "Rideable — pack for a change in the weather."
    if worst_score >= 40:
        return "Marginal. Expect at least one unpleasant stretch."
    if worst_score >= 20:
        return "Poor. Consider re-timing or re-routing."
    return "Do not ride this as planned."


async def _fetch(
    planned: Sequence[tuple[float, float, float, datetime]],
    settings: Settings,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Call Open-Meteo, in chunks, and return one hourly series per sample."""
    series: list[dict[str, Any]] = []
    forecast_days = _forecast_days(planned)

    # Open-Meteo accepts a comma-separated list of coordinates; keep the chunks
    # modest so a URL stays a sane length and a failure loses less work.
    chunk_size = 25
    for start in range(0, len(planned), chunk_size):
        chunk = planned[start : start + chunk_size]
        params = {
            "latitude": ",".join(f"{lat:.4f}" for lat, _, _, _ in chunk),
            "longitude": ",".join(f"{lon:.4f}" for _, lon, _, _ in chunk),
            "hourly": ",".join(_HOURLY_VARIABLES),
            "timezone": "UTC",
            "forecast_days": str(forecast_days),
        }
        response = await client.get(settings.weather_url, params=params)
        response.raise_for_status()
        payload = response.json()

        # With one coordinate the API returns an object; with several, an array.
        entries = payload if isinstance(payload, list) else [payload]
        if len(entries) != len(chunk):
            raise ValueError(
                f"Weather API returned {len(entries)} series for {len(chunk)} points."
            )
        series.extend(entries)
    return series


def _forecast_days(planned: Sequence[tuple[float, float, float, datetime]]) -> int:
    """How many days of hourly data we need to cover the last arrival."""
    last_eta = max(eta for _, _, _, eta in planned)
    now = datetime.now(timezone.utc)
    span_days = math.ceil((last_eta - now).total_seconds() / 86400.0) + 1
    # The free tier serves up to 16 days; anything beyond is guesswork anyway.
    return max(1, min(16, span_days))


def _build_point(
    lat: float,
    lon: float,
    distance_m: float,
    eta: datetime,
    series: dict[str, Any],
) -> WeatherPoint:
    point = WeatherPoint(lat=lat, lon=lon, distance_m=distance_m, eta=eta.isoformat())

    hourly = series.get("hourly") or {}
    times = hourly.get("time") or []
    index = _nearest_hour_index(times, eta)
    if index is None:
        point.description = "No forecast for this time"
        return point

    def value(name: str) -> Any:
        column = hourly.get(name) or []
        return column[index] if index < len(column) else None

    point.forecast_hour = times[index]
    point.temperature_c = value("temperature_2m")
    point.apparent_c = value("apparent_temperature")
    point.precipitation_mm = value("precipitation")
    probability = value("precipitation_probability")
    point.precipitation_probability = int(probability) if probability is not None else None
    point.wind_kmh = value("wind_speed_10m")
    point.gust_kmh = value("wind_gusts_10m")
    point.visibility_m = value("visibility")
    code = value("weather_code")
    point.weather_code = int(code) if code is not None else None
    # Note the explicit None check: code 0 is "clear sky", the best weather
    # there is, and `code or default` would quietly discard it as falsy.
    point.description = (
        WMO_DESCRIPTIONS.get(point.weather_code, "Unknown")
        if point.weather_code is not None
        else "No forecast"
    )

    point.rideability, point.warnings = rideability(
        temperature_c=point.temperature_c,
        precipitation_mm=point.precipitation_mm,
        precipitation_probability=point.precipitation_probability,
        gust_kmh=point.gust_kmh,
        visibility_m=point.visibility_m,
        weather_code=point.weather_code,
    )
    return point


def _nearest_hour_index(times: Sequence[str], eta: datetime) -> int | None:
    """Index of the hourly slot closest to the ETA.

    Open-Meteo returns naive local timestamps for the requested timezone; we ask
    for UTC, so they are compared as UTC.
    """
    if not times:
        return None
    target = eta.astimezone(timezone.utc).replace(tzinfo=None)

    best_index, best_delta = None, None
    for index, raw in enumerate(times):
        try:
            moment = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        delta = abs((moment - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best_index, best_delta = index, delta
    return best_index


def _cache_key(planned: Sequence[tuple[float, float, float, datetime]]) -> str:
    """Key on rounded coordinates and whole hours.

    Two plans that differ by a few metres or a few minutes want the same
    forecast; rounding makes them share a cache entry instead of each costing a
    request.
    """
    parts = [
        f"{lat:.2f},{lon:.2f}@{eta.astimezone(timezone.utc).strftime('%Y-%m-%dT%H')}"
        for lat, lon, _, eta in planned
    ]
    return "weather|" + "|".join(parts)
