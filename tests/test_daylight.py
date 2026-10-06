"""Sun position and where the light changes along a route.

The reference times below come from the `astral` library, computed once and
written in, so the suite does not depend on it. Two implementations agreeing
to the minute is the check: either could be wrong alone, both wrong the same
way is unlikely.
"""

from datetime import datetime, timedelta, timezone

import pytest

from moto_route import sun
from moto_route.config import Settings
from moto_route.models import GeoPoint, Route
from moto_route.services import daylight

UTC = timezone.utc
ATHENS = (37.98, 23.73)
KRAKOW = (50.06, 19.94)


def _close(actual, expected, seconds=60):
    assert actual is not None
    assert abs((actual - expected).total_seconds()) <= seconds, (actual, expected)


# ------------------------------------------------------------------ the sun

def test_sunrise_and_sunset_agree_with_an_independent_almanac():
    """The departure day itself, and the June trip's furthest north."""
    october = datetime(2026, 10, 23, 12, tzinfo=UTC)
    _close(sun.sunrise(*ATHENS, october), datetime(2026, 10, 23, 4, 41, 30, tzinfo=UTC))
    _close(sun.sunset(*ATHENS, october), datetime(2026, 10, 23, 15, 36, 43, tzinfo=UTC))

    june = datetime(2027, 6, 21, 12, tzinfo=UTC)
    _close(sun.sunrise(*KRAKOW, june), datetime(2027, 6, 21, 2, 30, 53, tzinfo=UTC))
    _close(sun.sunset(*KRAKOW, june), datetime(2027, 6, 21, 18, 53, 6, tzinfo=UTC))


def test_the_end_of_usable_light_agrees_too():
    """Civil dusk -- the line a rider cares about more than sunset itself."""
    noon = sun.solar_noon(*ATHENS, datetime(2026, 10, 23, 12, tzinfo=UTC))
    dark = sun.crossing(*ATHENS, noon, noon + timedelta(hours=12), sun.CIVIL_DARK_DEG)
    _close(dark, datetime(2026, 10, 23, 16, 4, 1, tzinfo=UTC))


def test_the_four_kinds_of_light_are_told_apart():
    """Same sun angle, morning and evening, are not the same warning.

    Twilight in the morning gets better as you ride on; in the evening it gets
    worse. Calling both "twilight" would hide which one you are riding into.
    """
    on = lambda h, m: datetime(2026, 10, 23, h, m, tzinfo=UTC)  # noqa: E731
    assert sun.light(*ATHENS, on(10, 0)) == "day"
    assert sun.light(*ATHENS, on(15, 50)) == "dusk"     # after 15:36 sunset
    assert sun.light(*ATHENS, on(17, 0)) == "night"
    assert sun.light(*ATHENS, on(4, 25)) == "dawn"      # before 04:41 sunrise


def test_a_sun_that_never_sets_does_not_invent_a_sunset():
    """Svalbard in June: no crossing to find, so the answer is None, not noon."""
    assert sun.sunset(78.2, 15.6, datetime(2027, 6, 21, 12, tzinfo=UTC)) is None


# --------------------------------------------------------- along the route

def _ride_north_from_athens(points=201) -> Route:
    line = [GeoPoint(lat=37.98 + i * 0.0091, lon=23.73 - i * 0.0045) for i in range(points)]
    return Route(name="t", source_format="gpx", lines=[line], waypoints=[])


def test_a_late_start_crosses_sunset_and_then_dark_in_that_order():
    route = _ride_north_from_athens()
    leaving = datetime(2026, 10, 23, 13, 30, tzinfo=UTC)          # 16:30 in Athens

    result = daylight.along_route(route, leaving, 65, Settings())
    events = [c["event"] for c in result["changes"]]
    distances = [c["distance_m"] for c in result["changes"]]

    assert events == ["sunset", "dark"]
    assert distances == sorted(distances)
    assert result["start"]["light"] == "day"
    assert result["end"]["light"] == "night"
    # Exactly the timing model the weather uses: 65 km/h from 13:30.
    sunset = result["changes"][0]
    expected = leaving + timedelta(hours=sunset["distance_m"] / 1000 / 65)
    _close(datetime.fromisoformat(sunset["eta"]), expected, seconds=5)


def test_an_early_start_finds_first_light_then_sunrise():
    route = _ride_north_from_athens()
    leaving = datetime(2026, 10, 23, 2, 30, tzinfo=UTC)           # 05:30 in Athens

    result = daylight.along_route(route, leaving, 65, Settings())

    assert result["start"]["light"] == "night"
    assert [c["event"] for c in result["changes"]] == ["first light", "sunrise"]


def test_a_tight_finish_reports_how_little_daylight_is_left():
    """Arriving 39 minutes before sunset is fine -- until lunch runs long."""
    route = _ride_north_from_athens()
    leaving = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

    result = daylight.along_route(route, leaving, 65, Settings())

    assert result["changes"] == []
    assert result["end"]["light"] == "day"
    assert result["end"]["margin_min"] is not None
    assert 0 < result["end"]["margin_min"] < 60


def test_daylight_needs_no_network_and_no_forecast():
    """It must answer exactly when the weather cannot.

    Offline mode, and a departure months past the 16-day forecast horizon: both
    make the weather service refuse. Neither touches the sun.
    """
    route = _ride_north_from_athens()
    leaving = datetime.now(UTC) + timedelta(days=200)

    result = daylight.along_route(route, leaving, 65, Settings(offline=True))

    assert result["available"] is True


def test_light_at_walks_the_changes_in_order():
    result = {"available": True, "start": {"light": "day"},
              "changes": [{"distance_m": 1000, "light": "dusk"},
                          {"distance_m": 5000, "light": "night"}]}

    assert daylight.light_at(result, 0) == "day"
    assert daylight.light_at(result, 1000) == "dusk"
    assert daylight.light_at(result, 4999) == "dusk"
    assert daylight.light_at(result, 9000) == "night"
    assert daylight.light_at(None, 9000) is None
