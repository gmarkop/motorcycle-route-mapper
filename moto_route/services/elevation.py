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


def gradient_at(
    samples: Sequence[dict[str, Any]],
    distances: Sequence[float],
) -> list[float]:
    """Interpolate gradient from an elevation profile onto other distances.

    The curviness profile and the elevation profile are sampled independently —
    one follows the shape of the road, the other a fixed spacing — so their
    points do not line up. Interpolating beats snapping to the nearest: a
    gradient is a property of a stretch of road, not of a point on it.
    """
    if not samples:
        return [0.0] * len(distances)

    marks = [(float(s["distance_m"]), float(s["gradient_pct"])) for s in samples]
    out: list[float] = []
    j = 0
    for distance in distances:
        while j + 2 < len(marks) and marks[j + 1][0] < distance:
            j += 1
        left, right = marks[j], marks[min(j + 1, len(marks) - 1)]
        span = right[0] - left[0]
        if span <= 0:
            out.append(left[1])
        else:
            t = min(max((distance - left[0]) / span, 0.0), 1.0)
            out.append(round(left[1] + (right[1] - left[1]) * t, 1))
    return out


def demanding_stretches(
    samples: Sequence[dict[str, Any]],
    settings: Settings,
) -> list[dict[str, Any]]:
    """Runs of road that are both twisty and steep.

    Deliberately not a blended "difficulty score". A single number that mixes
    curvature and gradient looks authoritative and hides which of the two drove
    it, and the rider cannot act on it. What is worth knowing is the
    combination itself — that the switchbacks at km 34 are on the way *down* —
    because that is what changes gear choice, braking and how early you slow.

    A stretch has to hold for ``demanding_min_m`` to count, so a single noisy
    sample on a bridge does not become a warning.
    """
    runs: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def close(run: list[dict[str, Any]]) -> None:
        if not run:
            return
        length = run[-1]["distance_m"] - run[0]["distance_m"]
        if length < settings.demanding_min_m:
            return
        # The worst values in the run describe it better than the mean: what
        # matters is the hardest moment, not the average of a hard stretch.
        peak_curve = max(r["curviness"] for r in run)
        peak_slope = max((r["gradient_pct"] for r in run), key=abs)
        runs.append({
            "from_m": round(run[0]["distance_m"]),
            "to_m": round(run[-1]["distance_m"]),
            "length_m": round(length),
            "curviness": round(peak_curve, 1),
            "gradient_pct": peak_slope,
            "descending": peak_slope < 0,
        })

    for sample in samples:
        steep = abs(sample.get("gradient_pct") or 0.0) >= settings.demanding_gradient_pct
        twisty = (sample.get("curviness") or 0.0) >= settings.demanding_curviness
        if steep and twisty:
            current.append(sample)
        else:
            close(current)
            current = []
    close(current)
    return runs
