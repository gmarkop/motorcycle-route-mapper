"""Weather service tests.

The live API is never called: an ``httpx.MockTransport`` answers with a recorded
response shape, which keeps the tests fast, offline and deterministic. That
matters more than usual here, because the thing under test is a *judgement* —
the rideability score — and a judgement can only be tested against fixed input.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from moto_route.config import Settings
from moto_route.models import GeoPoint, Route
from moto_route.services import weather
from moto_route.services.cache import TTLCache


# ------------------------------------------------------------------- fixtures

@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path, weather_interval_m=25_000, max_weather_samples=10)


@pytest.fixture
def cache(tmp_path) -> TTLCache:
    return TTLCache(tmp_path, "weather-test")


@pytest.fixture
def long_route() -> Route:
    # ~110 km due north, so sampling has something to sample.
    points = [GeoPoint(lat=48.0 + i * 0.01, lon=11.0) for i in range(100)]
    return Route(name="Test ride", lines=[points])


def hourly_series(start: datetime, hours: int = 48, **overrides):
    """Build an Open-Meteo style response for one coordinate."""
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(hours)]
    series = {
        "time": times,
        "temperature_2m": [18.0] * hours,
        "apparent_temperature": [17.0] * hours,
        "precipitation": [0.0] * hours,
        "precipitation_probability": [5] * hours,
        "weather_code": [1] * hours,
        "wind_speed_10m": [10.0] * hours,
        "wind_gusts_10m": [15.0] * hours,
        "visibility": [20000.0] * hours,
    }
    series.update(overrides)
    return {"hourly": series}


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------- scoring rules

def test_perfect_conditions_score_full_marks():
    score, warnings = weather.rideability(
        temperature_c=20.0, precipitation_mm=0.0, precipitation_probability=0,
        gust_kmh=10.0, visibility_m=20000, weather_code=0,
    )
    assert score == 100
    assert warnings == []


def test_ice_risk_dominates_every_other_factor():
    """Freezing rain has to outrank a merely wet day, whatever else is true."""
    icy, warnings = weather.rideability(
        temperature_c=1.0, precipitation_mm=0.5, precipitation_probability=90,
        gust_kmh=10.0, visibility_m=20000, weather_code=66,
    )
    wet, _ = weather.rideability(
        temperature_c=14.0, precipitation_mm=3.0, precipitation_probability=90,
        gust_kmh=10.0, visibility_m=20000, weather_code=65,
    )
    assert icy < wet
    assert icy < 40
    assert any("ice" in w.lower() for w in warnings)


def test_gusts_are_penalised_more_than_steady_wind():
    calm, _ = weather.rideability(
        temperature_c=18.0, precipitation_mm=0.0, precipitation_probability=0,
        gust_kmh=20.0, visibility_m=20000, weather_code=1,
    )
    gusty, warnings = weather.rideability(
        temperature_c=18.0, precipitation_mm=0.0, precipitation_probability=0,
        gust_kmh=70.0, visibility_m=20000, weather_code=1,
    )
    assert gusty < calm
    assert any("gust" in w.lower() or "crosswind" in w.lower() for w in warnings)


def test_thunderstorms_and_fog_both_register():
    storm, storm_warnings = weather.rideability(
        temperature_c=22.0, precipitation_mm=0.0, precipitation_probability=0,
        gust_kmh=10.0, visibility_m=20000, weather_code=95,
    )
    fog, fog_warnings = weather.rideability(
        temperature_c=12.0, precipitation_mm=0.0, precipitation_probability=0,
        gust_kmh=5.0, visibility_m=300, weather_code=45,
    )
    assert storm < 80 and any("hunder" in w for w in storm_warnings)
    assert fog < 90 and any("isibility" in w for w in fog_warnings)


def test_heat_is_penalised_but_less_than_ice():
    hot, warnings = weather.rideability(
        temperature_c=39.0, precipitation_mm=0.0, precipitation_probability=0,
        gust_kmh=10.0, visibility_m=20000, weather_code=0,
    )
    assert 60 < hot < 100
    assert any("Heat" in w for w in warnings)


def test_score_is_clamped_to_the_zero_hundred_range():
    worst, _ = weather.rideability(
        temperature_c=-5.0, precipitation_mm=12.0, precipitation_probability=100,
        gust_kmh=120.0, visibility_m=50, weather_code=99,
    )
    assert 0 <= worst <= 100


def test_missing_data_does_not_crash_the_score():
    score, _ = weather.rideability(
        temperature_c=None, precipitation_mm=None, precipitation_probability=None,
        gust_kmh=None, visibility_m=None, weather_code=None,
    )
    assert score == 100


# ---------------------------------------------------------------- ETA planning

def test_samples_get_increasing_arrival_times(long_route, settings):
    departure = datetime(2026, 6, 1, 8, tzinfo=timezone.utc)
    planned = weather.plan_samples(long_route, departure, speed_kmh=60, settings=settings)

    assert len(planned) >= 2
    etas = [eta for _, _, _, eta in planned]
    assert etas == sorted(etas)
    assert etas[0] == departure


def test_eta_matches_distance_over_speed(long_route, settings):
    departure = datetime(2026, 6, 1, 8, tzinfo=timezone.utc)
    planned = weather.plan_samples(long_route, departure, speed_kmh=60, settings=settings)
    _, _, distance_m, eta = planned[-1]

    expected_hours = (distance_m / 1000) / 60
    actual_hours = (eta - departure).total_seconds() / 3600
    assert actual_hours == pytest.approx(expected_hours, rel=1e-6)


def test_a_slower_rider_arrives_later(long_route, settings):
    departure = datetime(2026, 6, 1, 8, tzinfo=timezone.utc)
    fast = weather.plan_samples(long_route, departure, 100, settings)[-1][3]
    slow = weather.plan_samples(long_route, departure, 40, settings)[-1][3]
    assert slow > fast


def test_sample_count_respects_the_configured_cap(long_route, settings):
    settings.max_weather_samples = 4
    planned = weather.plan_samples(long_route, datetime.now(timezone.utc), 60, settings)
    assert len(planned) <= 5


# -------------------------------------------------------------- forecast fetch

async def test_forecast_returns_one_scored_point_per_sample(long_route, settings, cache):
    departure = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        count = len(request.url.params["latitude"].split(","))
        return httpx.Response(200, json=[hourly_series(departure) for _ in range(count)])

    async with make_client(handler) as client:
        result = await weather.forecast_along_route(
            long_route, departure, 60, settings, client, cache
        )

    assert result["available"] is True
    assert len(result["points"]) == len(
        weather.plan_samples(long_route, departure, 60, settings)
    )
    assert all(p["rideability"] == 100 for p in result["points"])
    assert len(requests) == 1, "all sample coordinates belong in one batched request"


async def test_forecast_picks_the_hour_the_rider_arrives(long_route, settings, cache):
    """The whole point of the feature: hour 3 of the ride reads hour 3's rain."""
    departure = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(request.url.params["latitude"].split(","))
        # Rain arrives 1 hour after departure and never stops.
        precipitation = [0.0] + [5.0] * 47
        return httpx.Response(200, json=[
            hourly_series(departure, precipitation=precipitation,
                          precipitation_probability=[10] + [95] * 47)
            for _ in range(count)
        ])

    async with make_client(handler) as client:
        result = await weather.forecast_along_route(
            long_route, departure, 60, settings, client, cache
        )

    points = result["points"]
    assert points[0]["precipitation_mm"] == 0.0, "dry at the start"
    assert points[-1]["precipitation_mm"] == 5.0, "soaked by the end"
    assert points[-1]["rideability"] < points[0]["rideability"]


async def test_summary_reports_the_worst_stretch(long_route, settings, cache):
    departure = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(request.url.params["latitude"].split(","))
        return httpx.Response(200, json=[
            hourly_series(departure, temperature_2m=[0.0] * 48,
                          precipitation=[2.0] * 48, weather_code=[71] * 48)
            for _ in range(count)
        ])

    async with make_client(handler) as client:
        result = await weather.forecast_along_route(
            long_route, departure, 60, settings, client, cache
        )

    summary = result["summary"]
    assert summary["worst_rideability"] < 40
    assert "not ride" in summary["verdict"].lower() or "Poor" in summary["verdict"]
    assert any("ice" in w.lower() for w in summary["warnings"])


async def test_a_failed_request_falls_back_to_the_cached_forecast(long_route, settings, cache):
    departure = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            count = len(request.url.params["latitude"].split(","))
            return httpx.Response(200, json=[hourly_series(departure) for _ in range(count)])
        raise httpx.ConnectError("network down")

    async with make_client(handler) as client:
        first = await weather.forecast_along_route(
            long_route, departure, 60, settings, client, cache
        )
        assert first["available"] is True

        # Expire the entry so the next call goes to the network and fails.
        settings.weather_ttl_s = 0
        cache.set(weather._cache_key(
            weather.plan_samples(long_route, departure, 60, settings)
        ), first, ttl_s=-1)

        second = await weather.forecast_along_route(
            long_route, departure, 60, settings, client, cache
        )

    assert second["stale"] is True
    assert second["points"] == first["points"]
    assert "failed" in second["reason"].lower()


async def test_a_failure_with_no_cache_reports_unavailable(long_route, settings, cache):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    async with make_client(handler) as client:
        result = await weather.forecast_along_route(
            long_route, datetime.now(timezone.utc), 60, settings, client, cache
        )

    assert result["available"] is False
    assert result["points"] == []
    assert "unreachable" in result["reason"].lower()


async def test_offline_mode_makes_no_request(long_route, settings, cache):
    settings.offline = True

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline mode must not touch the network")

    async with make_client(handler) as client:
        result = await weather.forecast_along_route(
            long_route, datetime.now(timezone.utc), 60, settings, client, cache
        )

    assert result["available"] is False
    assert "Offline" in result["reason"]


async def test_a_second_identical_request_is_served_from_cache(long_route, settings, cache):
    departure = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        count = len(request.url.params["latitude"].split(","))
        return httpx.Response(200, json=[hourly_series(departure) for _ in range(count)])

    async with make_client(handler) as client:
        for _ in range(3):
            await weather.forecast_along_route(
                long_route, departure, 60, settings, client, cache
            )

    assert calls["n"] == 1, "repeat plans must not re-hit the free API"


async def test_a_mismatched_response_length_is_rejected(long_route, settings, cache):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[hourly_series(datetime.now(timezone.utc))])

    async with make_client(handler) as client:
        result = await weather.forecast_along_route(
            long_route, datetime.now(timezone.utc), 60, settings, client, cache
        )

    assert result["available"] is False


async def test_clear_sky_is_not_reported_as_unknown(long_route, settings, cache):
    """WMO code 0 means 'clear sky'. It is also falsy, which is how it ends up
    being treated as missing data unless the lookup checks for None."""
    departure = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(request.url.params["latitude"].split(","))
        return httpx.Response(200, json=[
            hourly_series(departure, weather_code=[0] * 48) for _ in range(count)
        ])

    async with make_client(handler) as client:
        result = await weather.forecast_along_route(
            long_route, departure, 60, settings, client, cache
        )

    assert all(p["description"] == "Clear sky" for p in result["points"])


def test_every_wmo_code_the_api_can_return_has_a_description():
    # The codes Open-Meteo documents for its weather_code variable.
    documented = {0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
                  71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
    assert documented <= set(weather.WMO_DESCRIPTIONS)
