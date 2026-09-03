"""Check the Autobahn incident provider against the live API.

This provider was written from the documented shape of Germany's Autobahn GmbH
API and covered by tests using recorded fixtures, but the build environment has
no outbound network, so it has never seen real data. Everything it does is a
reasonable guess until someone runs it somewhere with a connection.

This script is that someone. Run it from a machine that can reach the internet:

    pip install httpx
    python tools/verify_autobahn.py                 # picks roads from the API
    python tools/verify_autobahn.py A8 A81          # or name them
    python tools/verify_autobahn.py --skip-overpass # no OSM detection check

It checks the assumptions the provider actually depends on, and — importantly —
runs the provider's own mapping code over the live payloads rather than a
re-implementation, so this cannot drift away from what the app really does.

Exit code 0 means the provider works against the live API today.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from moto_route.config import Settings  # noqa: E402
from moto_route.models import GeoPoint, Route  # noqa: E402
from moto_route.services.cache import TTLCache  # noqa: E402
from moto_route.services.incidents import AutobahnProvider  # noqa: E402

#: Default endpoint. Overridden by MOTO_AUTOBAHN_URL or --base-url, so this
#: verifies whatever endpoint the app is actually configured to talk to.
DEFAULT_BASE = "https://verkehr.autobahn.de/o/autobahn"

#: A few points along the A8 corridor between Stuttgart and Ulm, used only to
#: check that motorway refs can be detected from OpenStreetMap. Approximate on
#: purpose: a miss here is informational, not a failure.
A8_CORRIDOR = [(48.69, 9.20), (48.66, 9.35), (48.60, 9.60), (48.52, 9.78), (48.45, 9.90)]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, message: str) -> None:
        print(f"  {GREEN}PASS{RESET}  {message}")

    def fail(self, message: str) -> None:
        print(f"  {RED}FAIL{RESET}  {message}")
        self.failures.append(message)

    def warn(self, message: str) -> None:
        print(f"  {YELLOW}WARN{RESET}  {message}")
        self.warnings.append(message)

    def note(self, message: str) -> None:
        print(f"  {DIM}····  {message}{RESET}")


async def list_roads(client: httpx.AsyncClient, base: str, report: Report) -> list[str]:
    """The road index. The provider does not use it, but it names valid roads."""
    print("\nRoad index")
    try:
        response = await client.get(base)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - any failure is a finding here
        report.fail(f"could not reach {base}: {type(exc).__name__}: {exc}")
        return []

    if not isinstance(payload, dict):
        report.fail(f"expected a JSON object, got {type(payload).__name__}")
        return []

    report.note(f"top-level keys: {sorted(payload)}")
    roads = next((v for v in payload.values() if isinstance(v, list)), [])
    roads = [str(r) for r in roads if isinstance(r, (str, int))]
    if roads:
        report.ok(f"{len(roads)} roads listed, e.g. {roads[:6]}")
    else:
        report.warn("no list of roads found in the response")
    return roads


async def check_service(
    client: httpx.AsyncClient,
    provider: AutobahnProvider,
    base: str,
    road: str,
    service: str,
    report: Report,
    *,
    expect_germany: bool = True,
) -> int:
    """Check one road/service pair, and map its items with the real provider code."""
    url = f"{base.rstrip('/')}/{road}/services/{service}"
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        report.fail(f"{road}/{service}: request failed — {type(exc).__name__}: {exc}")
        return 0

    if not isinstance(payload, dict):
        report.fail(f"{road}/{service}: expected an object, got {type(payload).__name__}")
        return 0

    # Assumption 1: the payload key matches the service name.
    if service in payload:
        report.ok(f"{road}/{service}: payload key '{service}' present")
        items = payload[service]
    else:
        lists = [k for k, v in payload.items() if isinstance(v, list)]
        report.fail(
            f"{road}/{service}: no '{service}' key. Keys are {sorted(payload)}."
            + (f" The provider's fallback will use '{lists[0]}'." if lists else
               " There is no list to fall back to.")
        )
        items = payload[lists[0]] if lists else []

    if not isinstance(items, list):
        report.fail(f"{road}/{service}: value is {type(items).__name__}, not a list")
        return 0
    if not items:
        report.note(f"{road}/{service}: empty right now — nothing to inspect")
        return 0

    first = items[0]
    report.note(f"{road}/{service}: {len(items)} items; first has fields "
                f"{sorted(first) if isinstance(first, dict) else type(first).__name__}")

    # Assumption 2: coordinates live under 'coordinate', longitude spelled 'long'.
    coordinate = first.get("coordinate") if isinstance(first, dict) else None
    if isinstance(coordinate, dict):
        keys = sorted(coordinate)
        if "lat" in coordinate and "long" in coordinate:
            report.ok(f"{road}/{service}: coordinate keys {keys} — 'long' as expected")
        elif "lat" in coordinate and "lon" in coordinate:
            report.warn(
                f"{road}/{service}: coordinate uses 'lon', not 'long' (keys {keys}). "
                "The provider already accepts both, so this still works."
            )
        else:
            report.fail(f"{road}/{service}: coordinate keys are {keys}; expected lat + long")
    else:
        report.fail(f"{road}/{service}: no 'coordinate' object on the first item")

    # Assumption 3: description is a list of lines (a string is handled too).
    description = first.get("description") if isinstance(first, dict) else None
    if isinstance(description, list):
        report.ok(f"{road}/{service}: 'description' is a list, as assumed")
    elif isinstance(description, str):
        report.warn(f"{road}/{service}: 'description' is a string; the provider handles it")
    elif description is None:
        report.warn(f"{road}/{service}: no 'description' field")
    else:
        report.fail(f"{road}/{service}: 'description' is {type(description).__name__}")

    # Assumption 4 — the one that matters: the real mapping code produces
    # incidents with usable coordinates.
    mapped = [provider._item_to_incident(item, road, service) for item in items]
    good = [incident for incident in mapped if incident is not None]
    if len(good) == len(items):
        report.ok(f"{road}/{service}: all {len(items)} items mapped by the provider")
    elif good:
        report.warn(f"{road}/{service}: only {len(good)}/{len(items)} items mapped")
    else:
        report.fail(f"{road}/{service}: the provider mapped none of the {len(items)} items")

    if good:
        sample = good[0]
        report.note(f"    -> {sample.lat:.4f},{sample.lon:.4f} "
                    f"{sample.title[:50]!r} {sample.detail[:60]!r}")
        if not (-90 <= sample.lat <= 90 and -180 <= sample.lon <= 180):
            report.fail(f"{road}/{service}: mapped coordinates are out of range")
        # Germany, roughly. Catches a silent lat/long swap, which would otherwise
        # look like perfectly valid coordinates in the Indian Ocean. Only
        # meaningful against the real API; a stub's fixtures sit wherever their
        # author put them.
        elif not expect_germany:
            report.note(f"{road}/{service}: skipping the Germany bounds check "
                        "(not the public API)")
        elif not (47.0 <= sample.lat <= 55.5 and 5.5 <= sample.lon <= 15.5):
            report.fail(
                f"{road}/{service}: {sample.lat:.4f},{sample.lon:.4f} is outside "
                "Germany — latitude and longitude are probably swapped"
            )
        else:
            report.ok(f"{road}/{service}: mapped coordinates land inside Germany")

    return len(good)


async def check_detection(client: httpx.AsyncClient, provider: AutobahnProvider,
                          report: Report) -> None:
    """Does OpenStreetMap give back motorway refs the provider recognises?"""
    print("\nMotorway detection via OpenStreetMap")
    route = Route(name="A8 corridor",
                  lines=[[GeoPoint(lat=lat, lon=lon) for lat, lon in A8_CORRIDOR]])
    try:
        roads = await provider._detect_roads(route, client)
    except Exception as exc:  # noqa: BLE001
        report.fail(f"Overpass detection failed: {type(exc).__name__}: {exc}")
        return

    if roads:
        report.ok(f"detected {roads} from a route along the A8 corridor")
        if not any(r.startswith("A") for r in roads):
            report.fail("detected refs do not look like German motorways")
    else:
        report.warn(
            "no motorways detected. The test coordinates are approximate, so this "
            "may just mean they miss the carriageway — check a real German route "
            "in the app before treating it as a defect."
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roads", nargs="*", help="Roads to check, e.g. A8 A81")
    parser.add_argument("--skip-overpass", action="store_true",
                        help="Skip the OpenStreetMap ref-detection check")
    parser.add_argument("--max-roads", type=int, default=3,
                        help="How many roads to probe when none are named (default 3)")
    parser.add_argument("--base-url", default=None,
                        help="Endpoint to verify (default: MOTO_AUTOBAHN_URL, "
                             "else the public Autobahn API)")
    args = parser.parse_args()

    report = Report()
    settings = Settings()
    cache = TTLCache(None, "verify")
    provider = AutobahnProvider(settings, cache)
    base = args.base_url or settings.autobahn_url or DEFAULT_BASE

    print(f"Verifying the Autobahn provider against {base}")

    async with httpx.AsyncClient(timeout=30.0,
                                 headers={"User-Agent": settings.user_agent}) as client:
        available = await list_roads(client, base, report)
        roads = args.roads or available[:args.max_roads]
        if not roads:
            print(f"\n{RED}No roads to check.{RESET} Name some explicitly, e.g. A8.")
            return 1

        total = 0
        for road in roads:
            print(f"\nRoad {road}")
            for service in AutobahnProvider.SERVICES:
                total += await check_service(
                    client, provider, base, road, service, report,
                    expect_germany="autobahn.de" in base,
                )

        if not args.skip_overpass:
            await check_detection(client, provider, report)

    print("\n" + "=" * 66)
    print(f"Roads checked: {', '.join(roads)}   incidents mapped: {total}")
    if report.failures:
        print(f"{RED}{len(report.failures)} failure(s){RESET} — the provider does not "
              "match the live API:")
        for failure in report.failures:
            print(f"  - {failure}")
    if report.warnings:
        print(f"{YELLOW}{len(report.warnings)} warning(s){RESET} — handled, but worth knowing:")
        for warning in report.warnings:
            print(f"  - {warning}")
    if not report.failures:
        # Name the endpoint: "matches the live API" would be a lie when this was
        # pointed at a stub or a mirror.
        print(f"{GREEN}The provider matches {base}{RESET}")
        if total == 0:
            print("Note: no incidents were returned, so item mapping was never "
                  "exercised. Try again later, or name a busier road.")
    print("=" * 66)

    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
