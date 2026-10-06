"""Where the sun is, from date and position alone.

The NOAA solar calculator's equations (after Meeus, *Astronomical Algorithms*),
good to about a minute for sunrise and sunset at the latitudes this app rides
-- far finer than anything a rider would act on, and needing no network, no
API and no dependency. That last part matters more than the precision: a
daylight warning is wanted most for a ride planned weeks out, which is exactly
when the weather service has nothing to say.

Times in and out are UTC. Turning them into a clock a rider reads is the
caller's job, because only the caller knows whose clock it is.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

#: The sun's centre this far below the horizon is sunrise or sunset: half its
#: disc, plus the refraction that lets you see it after it has geometrically
#: set. The standard almanac value.
SUNSET_DEG = -0.833

#: Civil twilight ends here. Past it there is not enough light to read the road
#: without lamps, which is the line a rider cares about.
CIVIL_DARK_DEG = -6.0


def _julian_century(when: datetime) -> float:
    when = when.astimezone(timezone.utc)
    julian_day = when.timestamp() / 86400.0 + 2440587.5
    return (julian_day - 2451545.0) / 36525.0


def _declination_and_equation_of_time(when: datetime) -> tuple[float, float]:
    """Solar declination (radians) and the equation of time (minutes)."""
    t = _julian_century(when)

    mean_long = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0)
    mean_anom = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    centre = math.radians(
        math.sin(mean_anom) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * mean_anom) * (0.019993 - 0.000101 * t)
        + math.sin(3 * mean_anom) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * t)
    apparent_long = mean_long + centre - math.radians(0.00569 + 0.00478 * math.sin(omega))

    mean_obliquity = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))
                             / 60.0) / 60.0
    obliquity = math.radians(mean_obliquity + 0.00256 * math.cos(omega))

    declination = math.asin(math.sin(obliquity) * math.sin(apparent_long))

    y = math.tan(obliquity / 2.0) ** 2
    equation = 4.0 * math.degrees(
        y * math.sin(2 * mean_long)
        - 2 * ecc * math.sin(mean_anom)
        + 4 * ecc * y * math.sin(mean_anom) * math.cos(2 * mean_long)
        - 0.5 * y * y * math.sin(4 * mean_long)
        - 1.25 * ecc * ecc * math.sin(2 * mean_anom))
    return declination, equation


def elevation(lat: float, lon: float, when: datetime) -> float:
    """Height of the sun's centre above the horizon, in degrees.

    No refraction correction: the thresholds above already include it, which is
    how almanac sunrise and sunset are defined.
    """
    when = when.astimezone(timezone.utc)
    declination, equation = _declination_and_equation_of_time(when)

    minutes = when.hour * 60 + when.minute + when.second / 60.0
    solar_minutes = minutes + equation + 4.0 * lon          # longitude east-positive
    hour_angle = math.radians(solar_minutes / 4.0 - 180.0)

    lat_r = math.radians(lat)
    cos_zenith = (math.sin(lat_r) * math.sin(declination)
                  + math.cos(lat_r) * math.cos(declination) * math.cos(hour_angle))
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


def solar_noon(lat: float, lon: float, day: datetime) -> datetime:
    """When the sun is highest on this UTC calendar day, at this longitude."""
    midnight = day.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    _, equation = _declination_and_equation_of_time(midnight + timedelta(hours=12))
    return midnight + timedelta(minutes=720.0 - 4.0 * lon - equation)


def crossing(lat: float, lon: float, start: datetime, end: datetime,
             threshold: float) -> datetime | None:
    """When, between ``start`` and ``end``, the sun passes ``threshold``.

    Bisection rather than the closed-form hour-angle formula: it needs only
    :func:`elevation`, so there is one place the astronomy can be wrong rather
    than two that might disagree -- and the sign conventions of the closed form
    are exactly the kind of thing that is right in the notes and wrong in code.
    """
    above_start = elevation(lat, lon, start) > threshold
    if above_start == (elevation(lat, lon, end) > threshold):
        return None
    lo, hi = start, end
    for _ in range(40):                       # well under a second of precision
        mid = lo + (hi - lo) / 2
        if (elevation(lat, lon, mid) > threshold) == above_start:
            lo = mid
        else:
            hi = mid
    return lo + (hi - lo) / 2


def sunset(lat: float, lon: float, day: datetime) -> datetime | None:
    """Sunset on the UTC day containing ``day``; None if the sun never sets."""
    noon = solar_noon(lat, lon, day)
    return crossing(lat, lon, noon, noon + timedelta(hours=12), SUNSET_DEG)


def sunrise(lat: float, lon: float, day: datetime) -> datetime | None:
    """Sunrise on the UTC day containing ``day``; None if the sun never rises."""
    noon = solar_noon(lat, lon, day)
    return crossing(lat, lon, noon - timedelta(hours=12), noon, SUNSET_DEG)


def light(lat: float, lon: float, when: datetime) -> str:
    """"day", "dawn", "dusk" or "night" -- the four a rider tells apart.

    Twilight is split by which side of solar noon it falls on: the same sun
    angle means light arriving in the morning and light going in the evening,
    and only one of those gets worse as you ride on.
    """
    height = elevation(lat, lon, when)
    if height > SUNSET_DEG:
        return "day"
    if height <= CIVIL_DARK_DEG:
        return "night"
    return "dawn" if when.astimezone(timezone.utc) < solar_noon(lat, lon, when) else "dusk"
