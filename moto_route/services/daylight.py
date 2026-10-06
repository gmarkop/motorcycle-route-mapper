"""Where along the route the light goes, at the speed and start time planned.

Separate from the weather on purpose. The forecast needs a network and a date
inside its 16-day window; daylight needs neither. A warning that the last
80 km will be ridden in the dark is most useful for a trip planned weeks
ahead -- exactly when the weather service has nothing to say -- so it must not
be the weather service that says it.

It shares one thing with the weather: the timing. Both read their arrival
times from :func:`weather.plan_samples`, so "sunset at km 290" and "rain at
km 290" are about the same moment at the same place rather than two models
that happen to agree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import sun
from ..config import Settings
from ..models import Route
from . import weather

#: Which way the light changes when the sun passes each line. Going down
#: through the horizon is sunset; through -6 degrees is the end of usable
#: light. Coming back up, the same two lines in the opposite order.
_EVENTS = {
    (sun.SUNSET_DEG, "down"): ("sunset", "dusk"),
    (sun.CIVIL_DARK_DEG, "down"): ("dark", "night"),
    (sun.CIVIL_DARK_DEG, "up"): ("first light", "dawn"),
    (sun.SUNSET_DEG, "up"): ("sunrise", "day"),
}


def along_route(
    route: Route,
    departure: datetime,
    speed_kmh: float,
    settings: Settings,
) -> dict[str, Any]:
    """The light at the start and finish, and where in between it changes."""
    planned = weather.plan_samples(route, departure, speed_kmh, settings)
    if not planned:
        return {"available": False, "reason": "Route has no points.", "changes": []}

    samples = [(lat, lon, distance, eta) for lat, lon, distance, eta, _ in planned]
    changes: list[dict[str, Any]] = []
    for here, there in zip(samples, samples[1:]):
        for threshold in (sun.SUNSET_DEG, sun.CIVIL_DARK_DEG):
            change = _crossing_between(here, there, threshold)
            if change is not None:
                changes.append(change)
    changes.sort(key=lambda change: change["distance_m"])

    first, last = samples[0], samples[-1]
    end_light = sun.light(last[0], last[1], last[3])
    sunset_there = sun.sunset(last[0], last[1], last[3])
    margin = None
    if end_light == "day" and sunset_there is not None and sunset_there > last[3]:
        margin = round((sunset_there - last[3]).total_seconds() / 60.0)

    return {
        "available": True,
        "start": {"distance_m": round(first[2]), "eta": _iso(first[3]),
                  "light": sun.light(first[0], first[1], first[3])},
        "end": {"distance_m": round(last[2]), "eta": _iso(last[3]), "light": end_light,
                "sunset": _iso(sunset_there) if sunset_there else None,
                # How long you would have to spare at the far end. Worth saying
                # when it is small: an hour lost to a late start or a long lunch
                # moves the dark onto the road, and nothing else on the screen
                # would tell you so.
                "margin_min": margin},
        "changes": changes,
    }


def light_at(daylight: dict[str, Any] | None, distance_m: float) -> str | None:
    """The light at ``distance_m``, or None if daylight was never worked out."""
    if not daylight or not daylight.get("available"):
        return None
    state = (daylight.get("start") or {}).get("light")
    for change in daylight.get("changes") or []:
        if change["distance_m"] <= distance_m:
            state = change["light"]
        else:
            break
    return state


def _crossing_between(here, there, threshold: float) -> dict[str, Any] | None:
    """Where between two samples the sun passes ``threshold``, if it does.

    The samples are tens of kilometres apart, so the crossing is found by
    bisecting the stretch between them -- position and arrival time both move
    linearly along it, which is exactly the timing model the samples came from.
    Twilight lasts far longer than the gap between samples is ridden in, so a
    line is crossed at most once per gap.
    """
    def at(fraction: float):
        lat = here[0] + (there[0] - here[0]) * fraction
        lon = here[1] + (there[1] - here[1]) * fraction
        eta = here[3] + (there[3] - here[3]) * fraction
        return lat, lon, eta, sun.elevation(lat, lon, eta) > threshold

    above_here = at(0.0)[3]
    if above_here == at(1.0)[3]:
        return None

    lo, hi = 0.0, 1.0
    for _ in range(30):                    # metres along the stretch
        mid = (lo + hi) / 2
        if at(mid)[3] == above_here:
            lo = mid
        else:
            hi = mid
    fraction = (lo + hi) / 2
    lat, lon, eta, _ = at(fraction)
    event, light_after = _EVENTS[(threshold, "down" if above_here else "up")]
    return {
        "event": event,
        "light": light_after,
        "distance_m": round(here[2] + (there[2] - here[2]) * fraction),
        "eta": _iso(eta),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
    }


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")
