"""Check every external service the app depends on, from where it runs.

Written for exactly the situation that prompted it: the closures layer reported
a ConnectError while the points-of-interest layer, talking to the same Overpass
service, worked. That difference lives in the network between your machine and
those servers, which is somewhere the test suite cannot look.

    python tools/check_services.py
    python tools/check_services.py --route examples/dolomites_demo.gpx

It sends the app's *real* queries, not a token request — the closure query is
three times the size of the POI one and uses Overpass's heaviest output mode, so
a trivial ping would happily pass while the real thing is refused.

Exit code 0 if everything the app needs is reachable.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from moto_route import geo  # noqa: E402
import _env  # noqa: E402
from moto_route.config import Settings  # noqa: E402
from moto_route.parsers import parse_route_bytes  # noqa: E402
from moto_route.services import hazards, overpass, pois  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def line(state: str, label: str, detail: str = "") -> None:
    colour = {"ok": GREEN, "fail": RED, "warn": YELLOW}[state]
    tag = {"ok": "PASS", "fail": "FAIL", "warn": "WARN"}[state]
    print(f"  {colour}{tag}{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


async def timed(coro):
    started = time.perf_counter()
    try:
        return await coro, None, time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 - reporting is the whole job
        return None, exc, time.perf_counter() - started


async def check_overpass(settings: Settings, client: httpx.AsyncClient,
                         route, failures: list[str]) -> None:
    """Send the app's real queries, split the way the app splits them.

    This used to send the whole route as one query, which measured something
    the app never does: a 389 km route reported 39.5s when the app was actually
    issuing three chunks of at most 60 points each. Over-reporting the cost is
    almost as unhelpful as not measuring it.
    """
    print("\nOverpass (closures and points of interest)")
    coords = hazards._query_coordinates(route.all_latlon)
    chunks = overpass.chunk_coordinates(coords, settings.overpass_max_points)
    header = overpass.query_header(settings)

    builders = {
        "closure query": lambda chunk: hazards.build_query(
            chunk, settings.hazard_corridor_m, settings.max_hazards, header=header),
        "POI query": lambda chunk: pois.build_query(chunk, settings),
    }
    print(f"  {DIM}{len(coords)} query points -> {len(chunks)} chunk(s) of at most "
          f"{settings.overpass_max_points}, as the app sends them{RESET}")

    for endpoint in settings.overpass_endpoints:
        host = overpass._host(endpoint)
        print(f"  {DIM}via {host}{RESET}")
        single = Settings(
            overpass_url=endpoint, overpass_fallback_urls=[],
            overpass_timeout_s=settings.overpass_timeout_s, overpass_concurrency=1,
        )
        for label, build in builders.items():
            result, exc, seconds = await timed(
                overpass.run_chunked(chunks, build, single, client))
            if exc is not None:
                line("fail", label, f"{seconds:.1f}s — {exc}")
                failures.append(f"{host}: {label}")
                continue

            count = len(result.get("elements", []))
            detail = (f"{seconds:.1f}s across {len(chunks)} chunk(s), "
                      f"{count} elements")
            if result.get("partial"):
                line("warn", label,
                     detail + f" — {result['failed_chunks']} chunk(s) failed")
                failures.append(f"{host}: {label} (partial)")
            else:
                line("ok", label, detail)


def _stretch_of(points, target_km: float):
    """A run of roughly `target_km` taken from the middle of the route."""
    middle = len(points) // 2
    start = end = middle
    covered = 0.0
    while covered < target_km * 1000 and (start > 0 or end < len(points) - 1):
        if start > 0:
            start -= 1
            covered += geo.haversine_m(points[start], points[start + 1])
        if end < len(points) - 1:
            end += 1
            covered += geo.haversine_m(points[end - 1], points[end])
    return points[start:end + 1]


def describe_checkout(repo: Path) -> str:
    """Say which copy of the code is running.

    `install.sh` copies the repository to /opt/moto-route, so merging a fix and
    re-running from there silently exercises the old code — a check that was
    added and then simply did not appear in the output. Naming the path and the
    commit makes that obvious instead of mysterious.
    """
    import subprocess

    where = f"running from {repo}"
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%h %cs"],
            capture_output=True, text=True, timeout=5)
        if commit.returncode == 0 and commit.stdout.strip():
            return f"{where}  (commit {commit.stdout.strip()})"
    except Exception:  # noqa: BLE001 - a missing git is not worth reporting
        pass
    return (f"{where}  (not a git checkout — if this is an install.sh copy, "
            "re-run install.sh after merging changes)")


def load_route(route_path: Path):
    """Read the route file, explaining a refusal instead of raising a traceback.

    The permission case is the one that actually happens: this script needs no
    privileges at all, so people reasonably run it as the service user out of
    habit — and that account is deliberately shut out of home directories.
    """
    try:
        return parse_route_bytes(route_path.read_bytes(), route_path.name)
    except FileNotFoundError:
        print(f"{RED}No such file: {route_path}{RESET}")
        return None
    except IsADirectoryError:
        print(f"{RED}{route_path} is a directory, not a route file.{RESET}")
        return None
    except PermissionError:
        print(f"{RED}Cannot read {route_path}: permission denied.{RESET}\n")
        print("This script needs no privileges — it only reads that file and makes")
        print("outbound requests. If you ran it with `sudo -u motoroute`, that account")
        print("is a locked-down system user with no access to home directories.")
        print("\n  Run it as yourself instead:\n")
        print(f"      /opt/moto-route/.venv/bin/python tools/check_services.py "
              f"--route {route_path}\n")
        return None
    except Exception as exc:  # noqa: BLE001 - a bad route file is user input
        print(f"{RED}Could not read {route_path.name}: {exc}{RESET}")
        return None


async def check_corridor_semantics(settings: Settings, client: httpx.AsyncClient,
                                  route, failures: list[str]) -> None:
    """Does `around:` search the LINE through the coordinates, or just circles?

    The whole corridor design rests on the first reading. Douglas-Peucker leaves
    consecutive query points kilometres apart on a straight motorway — a
    perfectly straight route thins to its two endpoints — so if Overpass treats
    the list as separate 150 m circles instead of a polyline, almost the entire
    route goes unsearched and closures are missed in silence.

    The experiment: count highways along one stretch of the route twice, first
    with closely spaced coordinates and then with only that stretch's two ends.
    Same line either way. Similar counts mean a polyline; a collapse means
    circles.
    """
    print("\nCorridor semantics (does `around:` follow the line, or only the points?)")

    points = route.all_latlon
    if len(points) < 20:
        line("warn", "route too short to test", "needs a longer route")
        return

    # A SHORT stretch from the middle. The probe only has to make a gap
    # obvious, not cover the route: an earlier version took the middle half and
    # sent it unchunked, building a query more expensive than any the app
    # issues — it duly timed out without answering the question at all.
    stretch = _stretch_of(points, target_km=25.0)
    span_km = geo.total_distance_m(stretch) / 1000
    dense = geo.simplify(stretch, 150.0)[:settings.overpass_max_points]
    sparse = [stretch[0], stretch[-1]]
    gap_km = geo.haversine_m(sparse[0], sparse[1]) / 1000

    if len(dense) < 4 or gap_km < 2:
        line("warn", "no suitable stretch found",
             f"{span_km:.0f} km, {len(dense)} points")
        return

    def counting_query(coords):
        joined = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in coords)
        return (f"{overpass.query_header(settings)}"
                f'way(around:{int(settings.hazard_corridor_m)},{joined})["highway"];'
                "out count;")

    def total(payload):
        for element in payload.get("elements", []):
            if element.get("type") == "count":
                return int(element.get("tags", {}).get("total", 0))
        return None

    single = Settings(overpass_url=settings.overpass_endpoints[0],
                      overpass_fallback_urls=[], overpass_concurrency=1)

    dense_payload, exc, _ = await timed(overpass.run_query(counting_query(dense), single, client, attempts=1))
    if exc is not None:
        line("fail", "dense-coordinate count", str(exc)[:90])
        failures.append("corridor semantics probe")
        return
    sparse_payload, exc, _ = await timed(overpass.run_query(counting_query(sparse), single, client, attempts=1))
    if exc is not None:
        line("fail", "sparse-coordinate count", str(exc)[:90])
        failures.append("corridor semantics probe")
        return

    dense_total, sparse_total = total(dense_payload), total(sparse_payload)
    if dense_total is None or sparse_total is None:
        line("warn", "server did not return a count", "cannot tell; skipping")
        return

    line("ok", f"stretch of {span_km:.0f} km, endpoints {gap_km:.0f} km apart",
         f"{len(dense)} dense points vs 2")
    print(f"    highways found with dense coordinates: {dense_total}")
    print(f"    highways found with only the two ends: {sparse_total}")

    if dense_total == 0:
        line("warn", "no highways found either way", "inconclusive on this route")
    elif sparse_total >= dense_total * 0.8:
        line("ok", "`around:` follows the line",
             "the corridor is continuous, thinning is safe")
    else:
        line("fail", "`around:` searches the points, not the line",
             f"only {100 * sparse_total / dense_total:.0f}% found")
        failures.append(
            "CORRIDOR GAP: thinned coordinates leave most of a long route unsearched"
        )


async def check_simple(name: str, url: str, client: httpx.AsyncClient,
                       failures: list[str], params: dict | None = None) -> None:
    result, exc, seconds = await timed(client.get(url, params=params or {}, timeout=30.0))
    if exc is not None:
        line("fail", name, f"{seconds:.1f}s — {type(exc).__name__}: {exc}")
        failures.append(name)
    elif result.status_code >= 400:
        line("fail", name, f"HTTP {result.status_code}")
        failures.append(name)
    else:
        line("ok", name, f"{seconds:.1f}s, HTTP {result.status_code}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-corridor", action="store_true",
                        help="Skip the `around:` semantics probe (two extra queries)")
    parser.add_argument("--route", default=None,
                        help="GPX/KML to build the queries from "
                             "(default: examples/dolomites_demo.gpx)")
    args = parser.parse_args()

    # Before Settings(), so the deployment's own configuration is what gets
    # tested rather than the defaults.
    applied = _env.load()
    settings = Settings()
    repo = Path(__file__).resolve().parent.parent
    route_path = Path(args.route).expanduser() if args.route else repo / "examples" / "dolomites_demo.gpx"

    route = load_route(route_path)
    if route is None:
        return 2

    print(f"{DIM}{describe_checkout(repo)}{RESET}")
    if applied:
        print(f"{DIM}Loaded {len(applied)} setting(s) from {_env.ENV_FILE}{RESET}")
    # Named explicitly, because "which Overpass did this actually measure" is
    # the first thing to doubt when the numbers look surprising in either
    # direction.
    for index, endpoint in enumerate(settings.overpass_endpoints):
        role = "primary" if index == 0 else "fallback"
        print(f"{DIM}Overpass {role}: {endpoint}{RESET}")
    if settings.overpass_coverage_files:
        print(f"{DIM}Coverage: {len(settings.overpass_coverage_files)} "
              f"polygon file(s){RESET}")
    print(f"Checking the services this app needs, using {route_path.name} "
          f"({route.distance_m / 1000:.0f} km, "
          f"{len(geo.simplify(route.all_latlon, 250))} query points)")

    failures: list[str] = []
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent},
                                 follow_redirects=True) as client:
        await check_overpass(settings, client, route, failures)
        if not args.skip_corridor:
            await check_corridor_semantics(settings, client, route, failures)

        print("\nOther services")
        await check_simple("Open-Meteo (weather)", settings.weather_url, client, failures,
                           {"latitude": "48.1", "longitude": "11.6",
                            "hourly": "temperature_2m", "forecast_days": "1"})
        await check_simple(
            "OSRM (alternate routes)",
            f"{settings.osrm_url.rstrip('/')}/route/v1/driving/11.5,48.1;11.6,48.2",
            client, failures, {"overview": "false"})
        if settings.autobahn_enabled:
            await check_simple("Autobahn (German incidents)",
                               settings.autobahn_url, client, failures)

    print("\n" + "=" * 68)
    if failures:
        print(f"{RED}{len(failures)} service(s) unreachable:{RESET}")
        for failure in failures:
            print(f"  - {failure}")
        print("\nIf only the closure query fails, that server is refusing the "
              "heavy query specifically.\nA working mirror in "
              "MOTO_OVERPASS_FALLBACK_URLS is the fix; the app already rotates "
              "through them.")
        if any("CORRIDOR GAP" in f for f in failures):
            print("\nThe corridor failure is the serious one: it means long routes "
                  "are being searched\nonly near a few points, so closures are "
                  "missed without any error appearing.")
    else:
        print(f"{GREEN}Everything the app needs is reachable.{RESET}")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
