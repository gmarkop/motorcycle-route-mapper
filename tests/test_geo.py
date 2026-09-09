"""Geometry tests. These are the functions everything else trusts, so they are
checked against distances that can be verified by hand."""

import math
import random
import time

import pytest

from moto_route import geo


def test_haversine_matches_known_distance():
    # Greenwich to the Paris Observatory: ~334 km along a great circle.
    london = (51.4779, -0.0015)
    paris = (48.8355, 2.3363)
    distance = geo.haversine_m(london, paris)
    assert 330_000 < distance < 340_000


def test_haversine_is_symmetric_and_zero_for_identical_points():
    a, b = (48.0, 11.0), (48.5, 11.5)
    assert geo.haversine_m(a, b) == pytest.approx(geo.haversine_m(b, a))
    assert geo.haversine_m(a, a) == pytest.approx(0.0, abs=1e-6)


def test_one_degree_of_latitude_is_about_111km():
    assert geo.haversine_m((0.0, 0.0), (1.0, 0.0)) == pytest.approx(111_195, rel=0.001)


def test_bearing_cardinal_directions():
    assert geo.bearing_deg((0, 0), (1, 0)) == pytest.approx(0, abs=0.5)     # north
    assert geo.bearing_deg((0, 0), (0, 1)) == pytest.approx(90, abs=0.5)    # east
    assert geo.bearing_deg((1, 0), (0, 0)) == pytest.approx(180, abs=0.5)   # south


def test_cumulative_distances_start_at_zero_and_increase():
    points = [(48.0, 11.0), (48.1, 11.0), (48.2, 11.0)]
    cumulative = geo.cumulative_distances(points)
    assert len(cumulative) == len(points)
    assert cumulative[0] == 0.0
    assert cumulative[1] < cumulative[2]
    assert cumulative[2] == pytest.approx(geo.total_distance_m(points))


def test_bounding_box_with_margin_grows_the_box():
    points = [(48.0, 11.0), (48.2, 11.4)]
    tight = geo.bounding_box(points)
    loose = geo.bounding_box(points, margin_m=1000)
    assert tight == (48.0, 11.0, 48.2, 11.4)
    assert loose[0] < tight[0] and loose[2] > tight[2]


def test_bounding_box_rejects_empty_input():
    with pytest.raises(ValueError):
        geo.bounding_box([])


def test_point_to_segment_clamps_to_the_endpoints():
    a, b = (48.0, 11.0), (48.0, 11.1)
    # A point well past b measures to b itself, not to the infinite line.
    beyond = (48.0, 11.3)
    assert geo.point_to_segment_m(beyond, a, b) == pytest.approx(
        geo.haversine_m(beyond, b), rel=0.01
    )


def test_distance_to_polyline_finds_the_nearest_segment():
    line = [(48.0, 11.0), (48.0, 11.1), (48.1, 11.1)]
    # Sitting exactly on the middle vertex.
    assert geo.distance_to_polyline_m((48.0, 11.1), line) == pytest.approx(0.0, abs=1.0)
    # A degree of latitude is ~111 km; 0.01 deg north of the first leg is ~1.1 km.
    assert geo.distance_to_polyline_m((48.01, 11.05), line) == pytest.approx(1113, rel=0.05)


def test_simplify_drops_collinear_points_but_keeps_the_endpoints():
    straight = [(48.0, 11.0 + i * 0.01) for i in range(20)]
    assert geo.simplify(straight, tolerance_m=10) == [straight[0], straight[-1]]


def test_simplify_keeps_a_detour_larger_than_the_tolerance():
    points = [(48.0, 11.0), (48.05, 11.05), (48.0, 11.1)]
    assert len(geo.simplify(points, tolerance_m=100)) == 3
    # With a tolerance wider than the detour itself, the middle point goes.
    assert len(geo.simplify(points, tolerance_m=10_000)) == 2


def test_simplify_stays_within_tolerance_of_the_original():
    random.seed(7)
    lat, lon = 48.0, 11.0
    track = []
    for i in range(4000):
        lat += 0.00008 + random.gauss(0, 0.00002)
        lon += 0.00006 * math.sin(i / 300) + random.gauss(0, 0.00002)
        track.append((lat, lon))

    tolerance = 15.0
    simplified = geo.simplify(track, tolerance)
    assert len(simplified) < len(track) / 10  # worth doing at all

    worst = max(geo.distance_to_polyline_m(p, simplified) for p in track)
    # The radial pre-pass can add its own tolerance on top of the DP pass, so
    # twice the tolerance is the honest bound.
    assert worst <= tolerance * 2


def test_simplify_always_keeps_the_endpoints():
    points = [(48.0 + i * 0.001, 11.0 + i * 0.001) for i in range(50)]
    simplified = geo.simplify(points, tolerance_m=1000)
    assert simplified[0] == points[0]
    assert simplified[-1] == points[-1]


def test_radial_filter_removes_clustered_points():
    # Twenty points at a red light, then a real leg away from it.
    stationary = [(48.0 + i * 1e-6, 11.0) for i in range(20)]
    moving = [(48.0 + i * 0.001, 11.0) for i in range(1, 10)]
    kept = geo.radial_filter_indices(stationary + moving, min_dist_m=20.0)
    assert len(kept) < len(stationary + moving)
    assert kept[0] == 0
    assert kept[-1] == len(stationary + moving) - 1


def test_simplify_is_bounded_on_adversarial_input():
    """A zigzag where every point matters is Douglas-Peucker's worst case.

    Unbounded, this input takes minutes; the evaluation budget has to keep it
    to seconds even though the result is then coarser than the tolerance asks.
    """
    points = [(48.0 + i * 0.0001, 11.0 + (0.0001 if i % 2 else 0)) for i in range(20_000)]
    started = time.perf_counter()
    simplified = geo.simplify(points, tolerance_m=1.0)
    elapsed = time.perf_counter() - started

    assert elapsed < 10.0, f"simplification took {elapsed:.1f}s"
    assert len(simplified) >= 2


def test_douglas_peucker_budget_degrades_instead_of_failing():
    points = [(48.0 + i * 0.0001, 11.0 + (0.0001 if i % 2 else 0)) for i in range(2000)]
    generous = geo.douglas_peucker_indices(points, 0.5, max_evaluations=10_000_000)
    stingy = geo.douglas_peucker_indices(points, 0.5, max_evaluations=1_000)
    assert len(stingy) < len(generous)
    assert stingy[0] == 0 and stingy[-1] == len(points) - 1


def test_sample_every_includes_first_and_last_point():
    points = [(48.0 + i * 0.01, 11.0) for i in range(100)]  # ~110 km
    samples = geo.sample_every(points, interval_m=25_000)
    assert samples[0][0] == 0
    assert samples[-1][0] == len(points) - 1
    assert samples[0][1] == 0.0


def test_sample_every_respects_the_requested_spacing():
    points = [(48.0 + i * 0.01, 11.0) for i in range(100)]
    samples = geo.sample_every(points, interval_m=25_000)
    gaps = [b[1] - a[1] for a, b in zip(samples, samples[1:])]
    # Every gap except possibly the last (which runs into the end) is ~25 km.
    assert all(gap >= 20_000 for gap in gaps[:-1])


def test_sample_every_honours_the_sample_cap():
    points = [(48.0 + i * 0.001, 11.0) for i in range(5000)]
    samples = geo.sample_every(points, interval_m=100, max_samples=10)
    assert len(samples) <= 11  # the cap, plus the forced final point


def test_sample_every_on_a_single_point():
    assert geo.sample_every([(48.0, 11.0)], interval_m=1000) == [(0, 0.0)]
    assert geo.sample_every([], interval_m=1000) == []


def test_curviness_separates_a_motorway_from_a_pass():
    straight = [(48.0 + i * 0.005, 11.0) for i in range(60)]
    hairpins = [
        (48.0 + i * 0.002, 11.0 + (0.003 if i % 2 else -0.003))
        for i in range(60)
    ]
    assert geo.curviness_deg_per_km(straight) < 5
    assert geo.curviness_deg_per_km(hairpins) > 100


def test_curviness_of_a_degenerate_line_is_zero():
    assert geo.curviness_deg_per_km([(48.0, 11.0)]) == 0.0
    assert geo.curviness_deg_per_km([(48.0, 11.0), (48.1, 11.0)]) == 0.0


# ------------------------------------------------------- indexing the route

def _wandering_route(n=2000):
    import random
    random.seed(11)
    lat, lon = 38.0, 23.7
    out = []
    for _ in range(n):
        lat += 0.00028 + random.uniform(-0.00004, 0.00004)
        lon += random.uniform(-0.00006, 0.00006)
        out.append((lat, lon))
    return out


def test_the_index_agrees_with_scanning_every_segment():
    """The index is only worth having if it gives the same answers.

    Anything within the reach must match a full scan; anything beyond it may
    come back as infinity, because the caller is filtering by distance and
    "further than the corridor" is all it needs.
    """
    import random
    route = _wandering_route()
    index = geo.RouteIndex(route, 1000.0)
    cumulative = geo.cumulative_distances(route)
    random.seed(3)

    for i in range(60):
        anchor = route[(i * 31) % len(route)]
        probe = (anchor[0] + random.uniform(-0.004, 0.004),
                 anchor[1] + random.uniform(-0.004, 0.004))
        want_off, want_along = geo.project_onto_polyline(probe, route, cumulative)
        got_off, got_along = index.project(probe)

        if want_off <= 1000.0:
            assert got_off == pytest.approx(want_off, abs=0.5)
            assert got_along == pytest.approx(want_along, abs=1.0)
        else:
            assert got_off > 1000.0


def test_a_point_far_from_the_route_is_beyond_reach():
    route = _wandering_route(200)
    index = geo.RouteIndex(route, 200.0)

    assert index.distance_m((10.0, 10.0)) == float("inf")


def test_the_index_handles_a_single_point_route():
    index = geo.RouteIndex([(38.0, 23.7)], 500.0)
    assert index.project((38.0, 23.7))[0] == pytest.approx(0.0, abs=1.0)


def test_the_index_is_faster_than_scanning():
    """The point of the exercise: 235 stops against an 18,000-point track was
    four million segment tests, run synchronously inside an async handler."""
    import time
    route = _wandering_route(6000)
    probes = [(route[i * 97 % len(route)][0] + 0.001,
               route[i * 97 % len(route)][1] + 0.001) for i in range(120)]
    cumulative = geo.cumulative_distances(route)

    started = time.perf_counter()
    for p in probes:
        geo.project_onto_polyline(p, route, cumulative)
    scanning = time.perf_counter() - started

    index = geo.RouteIndex(route, 1000.0)
    started = time.perf_counter()
    for p in probes:
        index.project(p)
    indexed = time.perf_counter() - started

    assert indexed * 5 < scanning, (
        f"indexed {indexed:.3f}s vs scanning {scanning:.3f}s — no real gain")
