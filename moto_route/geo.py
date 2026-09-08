"""Geodesic helpers.

Everything here works on plain (lat, lon) tuples in degrees so the functions
stay usable from the parsers, the services and the tests without dragging the
domain models along.

Distances are metres. Where a flat-earth approximation is good enough (point to
segment distance over a few kilometres) we project to a local equirectangular
plane, which is far cheaper than repeated haversine calls and accurate to well
under a metre at motorcycle-route scales.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

LatLon = tuple[float, float]


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Great-circle distance between two (lat, lon) points, in metres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def bearing_deg(a: LatLon, b: LatLon) -> float:
    """Initial compass bearing from a to b, 0-360 degrees (0 = north)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def cumulative_distances(points: Sequence[LatLon]) -> list[float]:
    """Distance from the first point to each point, in metres.

    The returned list is the same length as ``points`` and starts with 0.0, so
    ``cumulative[-1]`` is the total length of the line.
    """
    out = [0.0]
    for prev, cur in zip(points, points[1:]):
        out.append(out[-1] + haversine_m(prev, cur))
    return out


def total_distance_m(points: Sequence[LatLon]) -> float:
    if len(points) < 2:
        return 0.0
    return cumulative_distances(points)[-1]


def bounding_box(points: Iterable[LatLon], margin_m: float = 0.0) -> tuple[float, float, float, float]:
    """Return (min_lat, min_lon, max_lat, max_lon), optionally grown by a margin."""
    lats, lons = [], []
    for lat, lon in points:
        lats.append(lat)
        lons.append(lon)
    if not lats:
        raise ValueError("bounding_box() needs at least one point")

    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    if margin_m > 0:
        dlat = margin_m / 111_320.0
        # A degree of longitude shrinks with latitude; use the widest edge so
        # the box is never too small.
        widest = max(abs(min_lat), abs(max_lat))
        dlon = margin_m / (111_320.0 * max(math.cos(math.radians(widest)), 0.01))
        min_lat, max_lat = min_lat - dlat, max_lat + dlat
        min_lon, max_lon = min_lon - dlon, max_lon + dlon

    return (min_lat, min_lon, max_lat, max_lon)


def _local_xy(point: LatLon, origin: LatLon) -> tuple[float, float]:
    """Project to metres on a plane tangent at ``origin`` (equirectangular)."""
    x = math.radians(point[1] - origin[1]) * math.cos(math.radians(origin[0])) * EARTH_RADIUS_M
    y = math.radians(point[0] - origin[0]) * EARTH_RADIUS_M
    return x, y


def project_on_segment(p: LatLon, a: LatLon, b: LatLon) -> tuple[float, float]:
    """Project ``p`` onto segment ``a``-``b``.

    Returns ``(distance_m, t)`` where ``t`` is the position of the closest point
    along the segment, clamped to ``[0, 1]`` — 0 at ``a``, 1 at ``b``. Callers
    that need only the distance use :func:`point_to_segment_m`; ``t`` is what
    turns "this hazard is 20 m off the road" into "at km 143.6".
    """
    px, py = _local_xy(p, a)
    bx, by = _local_xy(b, a)
    seg_len_sq = bx * bx + by * by
    if seg_len_sq == 0.0:
        return math.hypot(px, py), 0.0
    t = max(0.0, min(1.0, (px * bx + py * by) / seg_len_sq))
    dx, dy = px - t * bx, py - t * by
    return math.hypot(dx, dy), t


def point_to_segment_m(p: LatLon, a: LatLon, b: LatLon) -> float:
    """Shortest distance from point ``p`` to the segment ``a``-``b``, in metres."""
    return project_on_segment(p, a, b)[0]


def project_onto_polyline(
    point: LatLon,
    line: Sequence[LatLon],
    cumulative: Sequence[float] | None = None,
) -> tuple[float, float]:
    """Locate a point relative to a route.

    Returns ``(distance_off_line_m, distance_along_line_m)``. The along-distance
    interpolates *within* the winning segment rather than snapping to its nearer
    end, which matters on the long straight segments a simplified route is made
    of — snapping there can be off by hundreds of metres.

    Pass ``cumulative`` (from :func:`cumulative_distances`) when projecting many
    points onto the same line, so it is computed once rather than per point.
    """
    if not line:
        return float("inf"), 0.0
    if len(line) == 1:
        return haversine_m(point, line[0]), 0.0

    if cumulative is None:
        cumulative = cumulative_distances(line)

    best_off, best_along = float("inf"), 0.0
    for i, (a, b) in enumerate(zip(line, line[1:])):
        off, t = project_on_segment(point, a, b)
        if off < best_off:
            best_off = off
            best_along = cumulative[i] + t * (cumulative[i + 1] - cumulative[i])
    return best_off, best_along


def distance_to_polyline_m(p: LatLon, line: Sequence[LatLon]) -> float:
    """Shortest distance from ``p`` to a polyline. Used to filter hazards."""
    if not line:
        return float("inf")
    if len(line) == 1:
        return haversine_m(p, line[0])
    return min(point_to_segment_m(p, a, b) for a, b in zip(line, line[1:]))


#: Douglas-Peucker is O(n log n) on well-behaved input but degrades to O(n^2)
#: when almost every point survives — a track with GPS noise larger than the
#: tolerance does exactly that. The budget caps the work: once spent, the
#: remaining segments stop splitting. The result is a slightly coarser line, not
#: a wrong one, which is the right trade for a file we did not write.
MAX_SIMPLIFY_EVALUATIONS = 1_500_000


def radial_filter_indices(points: Sequence[LatLon], min_dist_m: float) -> list[int]:
    """Indices of points at least ``min_dist_m`` from the previously kept one.

    A cheap O(n) first pass. Its real job is removing the cluster of near
    identical points a GPS emits at a red light, which is both the most common
    redundancy in a recorded track and the input that makes Douglas-Peucker
    quadratic.
    """
    n = len(points)
    if n < 3 or min_dist_m <= 0:
        return list(range(n))

    keep = [0]
    anchor = points[0]
    for i in range(1, n - 1):
        if haversine_m(anchor, points[i]) >= min_dist_m:
            keep.append(i)
            anchor = points[i]
    keep.append(n - 1)
    return keep


def douglas_peucker_indices(
    points: Sequence[LatLon],
    tolerance_m: float,
    max_evaluations: int = MAX_SIMPLIFY_EVALUATIONS,
) -> list[int]:
    """Indices of the points Douglas-Peucker keeps.

    Every dropped point lies within ``tolerance_m`` of the simplified line —
    unless the evaluation budget runs out, in which case some segments are left
    unsplit and the line is coarser than requested.

    Implemented with an explicit stack instead of recursion so a long track
    cannot hit Python's recursion limit.
    """
    n = len(points)
    if n < 3 or tolerance_m <= 0:
        return list(range(n))

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    budget = max_evaluations

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue

        budget -= end - start - 1
        if budget < 0:
            break

        a, b = points[start], points[end]
        worst_dist, worst_idx = -1.0, -1
        for i in range(start + 1, end):
            d = point_to_segment_m(points[i], a, b)
            if d > worst_dist:
                worst_dist, worst_idx = d, i
        if worst_dist > tolerance_m:
            keep[worst_idx] = True
            stack.append((start, worst_idx))
            stack.append((worst_idx, end))

    return [i for i, k in enumerate(keep) if k]


def simplify_indices(points: Sequence[LatLon], tolerance_m: float) -> list[int]:
    """Indices of the points worth drawing, at the given tolerance.

    Two passes, in the order the classic simplify.js uses: a radial filter to
    throw away clustered points cheaply, then Douglas-Peucker on the survivors
    to preserve the shape. The pre-pass roughly doubles the worst-case error
    (a dropped point can be off by the tolerance from both passes) and in
    exchange makes a pathological track fast instead of unbounded.
    """
    n = len(points)
    if n < 3 or tolerance_m <= 0:
        return list(range(n))

    coarse = radial_filter_indices(points, tolerance_m)
    subset = [points[i] for i in coarse]
    return [coarse[k] for k in douglas_peucker_indices(subset, tolerance_m)]


def simplify(points: Sequence[LatLon], tolerance_m: float) -> list[LatLon]:
    """Simplified copy of a polyline. See :func:`simplify_indices`."""
    return [points[i] for i in simplify_indices(points, tolerance_m)]


def sample_every(
    points: Sequence[LatLon],
    interval_m: float,
    max_samples: int | None = None,
) -> list[tuple[int, float]]:
    """Pick points spaced roughly ``interval_m`` apart along the line.

    Returns ``(index, distance_from_start_m)`` pairs, always including the first
    and last point. Weather is requested at these samples rather than at every
    track point: one forecast per ~25 km is plenty, and it keeps us well inside
    the free API's fair-use limits.
    """
    if not points:
        return []
    if len(points) == 1:
        return [(0, 0.0)]

    cumulative = cumulative_distances(points)
    total = cumulative[-1]

    if max_samples is not None and max_samples >= 2:
        # Widen the interval rather than truncate the route, so the samples
        # still span the whole ride.
        interval_m = max(interval_m, total / (max_samples - 1))

    out: list[tuple[int, float]] = [(0, 0.0)]
    next_mark = interval_m
    for i, dist in enumerate(cumulative):
        if dist >= next_mark and i != len(points) - 1:
            out.append((i, dist))
            # Skip past any marks the previous gap jumped over.
            next_mark = (math.floor(dist / interval_m) + 1) * interval_m
    last = len(points) - 1
    if out[-1][0] != last:
        out.append((last, total))
    return out


def curviness_deg_per_km(points: Sequence[LatLon], min_segment_m: float = 40.0) -> float:
    """How twisty a line is: total heading change per kilometre.

    A motorway sits near 0-20, a decent country road around 60-150, and an
    Alpine pass runs into the hundreds. This is the metric a rider actually
    wants when comparing two ways round a valley — "5 minutes slower" says far
    less than "three times as many corners".

    Short segments are merged before measuring, because GPS jitter between two
    points 3 m apart produces large, meaningless heading changes.
    """
    if len(points) < 3:
        return 0.0

    # Resample onto vertices at least ``min_segment_m`` apart to suppress noise.
    anchors = [points[0]]
    for point in points[1:]:
        if haversine_m(anchors[-1], point) >= min_segment_m:
            anchors.append(point)
    if len(anchors) < 3:
        return 0.0

    total_turn = 0.0
    for a, b, c in zip(anchors, anchors[1:], anchors[2:]):
        delta = abs(bearing_deg(b, c) - bearing_deg(a, b))
        # Heading change wraps at 360; 350 degrees right is 10 degrees left.
        total_turn += min(delta, 360.0 - delta)

    length_km = total_distance_m(anchors) / 1000.0
    if length_km <= 0:
        return 0.0
    return total_turn / length_km


def curviness_profile(
    points: Sequence[LatLon],
    window_m: float = 600.0,
    min_segment_m: float = 40.0,
) -> list[tuple[int, float, float]]:
    """Curviness sampled *along* a route, for colouring it by how twisty it is.

    :func:`curviness_deg_per_km` reduces a whole route to one number, which is
    the wrong tool for a heat map: a ride that is motorway for 100 km and
    hairpins for 20 averages out to "mildly interesting" and hides both halves.
    This walks a sliding window along the line instead.

    Returns ``(index, distance_from_start_m, deg_per_km)`` per anchor, where
    ``index`` points back into ``points``. Anchors are spaced at least
    ``min_segment_m`` apart for the same reason as in
    :func:`curviness_deg_per_km`: heading change between two points 3 m apart is
    GPS noise, not a corner.
    """
    if len(points) < 3:
        return []

    # Anchors: thin the line so heading changes mean something.
    anchor_idx = [0]
    for i in range(1, len(points)):
        if haversine_m(points[anchor_idx[-1]], points[i]) >= min_segment_m:
            anchor_idx.append(i)
    if len(anchor_idx) < 3:
        return []

    anchors = [points[i] for i in anchor_idx]
    anchor_dist = cumulative_distances(anchors)

    # Heading change at each interior anchor.
    turns = [0.0] * len(anchors)
    for j in range(1, len(anchors) - 1):
        delta = abs(bearing_deg(anchors[j], anchors[j + 1]) - bearing_deg(anchors[j - 1], anchors[j]))
        turns[j] = min(delta, 360.0 - delta)

    # Sliding window sum, expanded symmetrically around each anchor. Two moving
    # pointers keep this linear rather than re-scanning the window each time.
    half = window_m / 2.0
    profile: list[tuple[int, float, float]] = []
    lo = hi = 0
    running = 0.0

    for j, centre in enumerate(anchor_dist):
        while hi < len(anchors) and anchor_dist[hi] <= centre + half:
            running += turns[hi]
            hi += 1
        while anchor_dist[lo] < centre - half:
            running -= turns[lo]
            lo += 1

        span_km = (anchor_dist[hi - 1] - anchor_dist[lo]) / 1000.0
        # A window shorter than half the target means we are at one end of the
        # route; reporting deg/km off a 50 m span would produce wild numbers.
        value = running / span_km if span_km >= (window_m / 2000.0) else 0.0
        profile.append((anchor_idx[j], anchor_dist[j], value))

    return profile


def _interpolate_along(points: Sequence[LatLon], spacing_m: float) -> list[LatLon]:
    """Walk the line emitting a point at least every ``spacing_m``.

    :func:`sample_every` can only return coordinates the route already has, so
    it cannot close a gap: a GPX whose points are 2 km apart still yields 2 km
    gaps. New points are interpolated along each segment instead. Over a short
    segment the straight line is a fair stand-in for the road, and that is what
    the file itself asserts.

    Deciding *which* segments are worth filling is the caller's job, because
    the answer depends on the recorded spacing and this function is handed a
    simplified line, where a 20 km chord may mean either "the road is straight
    here" or "the file says nothing here".
    """
    out: list[LatLon] = [points[0]]
    for a, b in zip(points, points[1:]):
        span = haversine_m(a, b)
        if span > spacing_m:
            steps = int(span // spacing_m)
            for k in range(1, steps + 1):
                t = k * spacing_m / span
                if t < 1.0:
                    out.append((a[0] + (b[0] - a[0]) * t,
                                a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return out


def corridor_points(
    points: Sequence[LatLon],
    radius_m: float,
    max_points: int | None = None,
    max_span_m: float = 5000.0,
) -> list[LatLon]:
    """Coordinates whose corridor genuinely covers the route.

    Overpass is asked for features within ``radius_m`` of a list of
    coordinates. Thinning that list is what makes a long route affordable, and
    thinning it too far is how a route stops being searched without anything
    saying so. Two conditions have to hold, and which one binds depends on a
    detail of Overpass worth not betting on:

    * If ``around:`` follows the line *between* coordinates, the thinned line
      must stay well inside the corridor. Douglas-Peucker with tolerance ``t``
      guarantees only that the road is within ``t`` of the line, so ``t`` has
      to be comfortably smaller than ``radius_m`` — a 250 m tolerance inside a
      150 m corridor left 100 m of real road outside it on every curve it cut.
    * If it searches near each coordinate instead, consecutive coordinates must
      be no more than ``2 * radius_m`` apart, or the circles do not touch and
      the road between them is never looked at.

    Satisfying both costs no more than satisfying the stricter one, so this
    does that and the question stops mattering.

    ``max_points`` is a ceiling for a caller that must bound a single query.
    Reaching it means the corridor is *not* fully covered, so callers that care
    about coverage should chunk instead of capping.
    """
    if len(points) < 2:
        return list(points)

    # Split where the file itself goes quiet, and treat each run separately.
    #
    # Two *recorded* points far apart say nothing about what lies between them:
    # the ferry from Igoumenitsa to Venice is one 925 km segment, and filling
    # that line would ask Overpass about 4,113 coordinates of open Adriatic in
    # 69 chunks. On land the same holds — a chord across 50 km of countryside
    # is not the road, and a corridor along it searches the wrong ground.
    #
    # The test has to be on the recorded spacing, not on the simplified line.
    # Simplifying collapses a genuinely straight 20 km road to its two ends,
    # and a chord that long then looks identical to a gap the file never
    # described. Getting that backwards dropped a well-described straight road
    # from 90 coordinates to 2.
    runs: list[list[LatLon]] = [[points[0]]]
    for a, b in zip(points, points[1:]):
        if haversine_m(a, b) > max_span_m:
            runs.append([b])
        else:
            runs[-1].append(b)

    if len(runs) > 1:
        out: list[LatLon] = []
        for run in runs:
            out.extend(corridor_points(run, radius_m, max_span_m=max_span_m))
        if max_points is not None and len(out) > max_points:
            step = len(out) / max_points
            out = [out[int(i * step)] for i in range(max_points)]
        return out

    # Thin first, then fill the gaps thinning left. The other order keeps every
    # recorded point on a straight road — a 1 Hz track log would send thousands
    # of coordinates 10 m apart to cover a corridor that needs one every 225 m.
    #
    # Simplifying to half the radius bounds how far the road can stray from the
    # line through these coordinates, so a cut curve stays inside the corridor.
    thinned = simplify(points, max(radius_m * 0.5, 1.0))

    merged = _interpolate_along(thinned, max(radius_m * 1.5, 1.0))

    if max_points is not None and len(merged) > max_points:
        step = len(merged) / max_points
        merged = [merged[int(i * step)] for i in range(max_points)]
    return merged
