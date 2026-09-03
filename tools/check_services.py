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

import httpx  # noqa: E402

from moto_route import geo  # noqa: E402
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
    print("\nOverpass (closures and points of interest)")
    coords = hazards._query_coordinates(route.all_latlon)
    header = overpass.query_header(settings)

    queries = {
        "closure query (heavy: 8 filters, out geom)":
            hazards.build_query(coords, settings.hazard_corridor_m,
                                settings.max_hazards, header=header),
        "POI query (lighter: 3 filters, out center)":
            pois.build_query(coords, settings),
    }

    for endpoint in settings.overpass_endpoints:
        host = overpass._host(endpoint)
        print(f"  {DIM}via {host}{RESET}")
        for label, query in queries.items():
            single = Settings(
                overpass_url=endpoint,
                overpass_fallback_urls=[],
                overpass_timeout_s=settings.overpass_timeout_s,
                overpass_concurrency=1,
            )
            result, exc, seconds = await timed(
                overpass.run_query(query, single, client, attempts=1))
            if exc is None:
                count = len(result.get("elements", []))
                line("ok", f"{label}", f"{seconds:.1f}s, {count} elements")
            else:
                line("fail", f"{label}", f"{seconds:.1f}s — {exc}")
                failures.append(f"{host}: {label}")


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
    parser.add_argument("--route", default=None,
                        help="GPX/KML to build the queries from "
                             "(default: examples/dolomites_demo.gpx)")
    args = parser.parse_args()

    settings = Settings()
    repo = Path(__file__).resolve().parent.parent
    route_path = Path(args.route) if args.route else repo / "examples" / "dolomites_demo.gpx"
    route = parse_route_bytes(route_path.read_bytes(), route_path.name)

    print(f"Checking the services this app needs, using {route_path.name} "
          f"({route.distance_m / 1000:.0f} km, "
          f"{len(geo.simplify(route.all_latlon, 250))} query points)")

    failures: list[str] = []
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent},
                                 follow_redirects=True) as client:
        await check_overpass(settings, client, route, failures)

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
    else:
        print(f"{GREEN}Everything the app needs is reachable.{RESET}")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
