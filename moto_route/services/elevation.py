"""Elevation along the route, from the file when it has it and a DEM when not.

A GPX recorded by a GPS carries `<ele>` on every point. A GPX produced by an
online converter from a Google Maps link almost never does — which is exactly
how this app's owner makes his routes, so the elevation profile was blank for
the files that matter most, and anything built on top of it would have been
blank too.

So when the file is silent, the terrain is asked instead. Open-Meteo serves the
Copernicus GLO-90 digital elevation model: free, no key, up to 100 coordinates
per request, and the same provider the weather layer already talks to. At 90 m
resolution it will not find the lip of one hairpin, but it is more than enough
for the gradient of the road over the length of a bend, which is the question
being asked.

Terrain does not change, so these answers are cached hard — the point of the
long TTL is that a route you rode last month costs nothing to look at again.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import httpx

from .. import geo
from ..config import Settings
from .cache import TTLCache

log = logging.getLogger(__name__)

#: Open-Meteo takes at most 100 coordinates in one elevation request.
MAX_PER_REQUEST = 100


class ElevationError(RuntimeError):
    """A failed elevation lookup, with a message fit to show a rider."""


def _cache_key(coords: Sequence[geo.LatLon]) -> str:
    # 4 decimal places is ~11 m, comfortably finer than the 90 m model, so two
    # requests for the same route share an entry instead of each costing a
    # round trip.
    return "|".join(f"{lat:.4f},{lon:.4f}" for lat, lon in coords)


async def lookup(
    coords: Sequence[geo.LatLon],
    settings: Settings,
    client: httpx.AsyncClient,
    cache: TTLCache,
) -> list[float]:
    """Ground elevation in metres for each coordinate, in order."""
    if not coords:
        return []

    key = _cache_key(coords)
    hit = cache.get(key)
    if hit is not None:
        return hit

    values: list[float] = []
    for start in range(0, len(coords), MAX_PER_REQUEST):
        batch = coords[start : start + MAX_PER_REQUEST]
        params = {
            "latitude": ",".join(f"{lat:.4f}" for lat, _ in batch),
            "longitude": ",".join(f"{lon:.4f}" for _, lon in batch),
        }
        try:
            response = await client.get(settings.elevation_url, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ElevationError(
                f"Could not reach the elevation service — {exc}."
            ) from exc
        except ValueError as exc:
            raise ElevationError("The elevation service did not return JSON.") from exc

        got = payload.get("elevation") if isinstance(payload, dict) else None
        if not isinstance(got, list) or len(got) != len(batch):
            # Silently short results would misalign every gradient after this
            # point, which is worse than saying the lookup failed.
            raise ElevationError(
                f"Elevation service returned {len(got) if isinstance(got, list) else 0}"
                f" values for {len(batch)} coordinates."
            )
        values.extend(float(v) for v in got)

    cache.set(key, values, settings.elevation_ttl_s)
    return values


def gradients(samples: Sequence[tuple[float, float]]) -> list[float]:
    """Signed gradient in percent at each (distance_m, elevation_m) sample.

    Centred on each sample rather than measured forwards, so a climb is not
    reported half a sample early, and so the first and last points get a
    one-sided answer instead of no answer.
    """
    if len(samples) < 2:
        return [0.0] * len(samples)

    out: list[float] = []
    for i in range(len(samples)):
        before = samples[max(i - 1, 0)]
        after = samples[min(i + 1, len(samples) - 1)]
        run = after[0] - before[0]
        # Two samples at the same distance would divide by zero; a flat answer
        # is the honest one, since there is no length to have climbed over.
        out.append(round((after[1] - before[1]) / run * 100.0, 1) if run > 0 else 0.0)
    return out


async def profile(
    points: Sequence[geo.LatLon],
    file_elevations: Sequence[float | None],
    settings: Settings,
    client: httpx.AsyncClient,
    cache: TTLCache,
) -> dict[str, Any]:
    """Distance/elevation/gradient samples for the whole route.

    ``file_elevations`` is what the GPX carried, one per point, with None where
    it carried nothing. When enough of it is present the file wins: it is the
    road's own height, where the model is the ground's.
    """
    if len(points) < 2:
        return {"available": False, "reason": "Route is too short to profile.",
                "samples": []}

    marks = geo.sample_every(points, settings.elevation_sample_m,
                             max_samples=settings.max_elevation_samples)
    from_file = [file_elevations[i] if i < len(file_elevations) else None
                 for i, _ in marks]

    if all(value is not None for value in from_file):
        heights = [float(value) for value in from_file]
        source = "file"
    elif settings.offline:
        # Offline mode means no outbound calls, so the file is all there is.
        return {"available": False, "samples": [],
                "reason": "This file has no elevation data, and offline mode "
                          "cannot ask the terrain model for it."}
    else:
        try:
            heights = await lookup([points[i] for i, _ in marks],
                                   settings, client, cache)
        except ElevationError as exc:
            log.warning("Elevation lookup failed: %s", exc)
            return {"available": False, "reason": str(exc), "samples": []}
        source = "dem"

    pairs = [(distance, height) for (_, distance), height in zip(marks, heights)]
    slopes = gradients(pairs)

    ascent = sum(max(b[1] - a[1], 0.0) for a, b in zip(pairs, pairs[1:]))
    descent = sum(max(a[1] - b[1], 0.0) for a, b in zip(pairs, pairs[1:]))

    return {
        "available": True,
        "source": source,
        "note": ("Heights from the GPX file." if source == "file" else
                 "Heights from the Copernicus GLO-90 terrain model (90 m), "
                 "because the file carried none."),
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "samples": [
            {"distance_m": round(distance), "ele": round(height, 1),
             "gradient_pct": slope}
            for (distance, height), slope in zip(pairs, slopes)
        ],
    }
