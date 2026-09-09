"""Count candidate map tags along a real route, before building a feature on them.

The plan is to add categories to the points-of-interest layer: motorcycle
shops, repair, accommodation, and the "motorcycle-friendly" cafes and hotels a
friend suggested. Which of those is worth a category is a question about how
much of each tag actually exists near roads this owner rides, and that is
measurable rather than arguable. `motorcycle_friendly=yes` is the cautionary
case -- its own wiki page calls it "rarely tagged and not used by any real data
consumer", so a category built on it would ship an empty panel.

    python tools/tag_census.py --route ~/Athens-Volos.gpx

**This asks a full-data server on purpose.** A local instance built by
build-extract.sh holds only the tags the app already queries, so every
candidate here would come back zero -- meaning "not imported", not "not there".
That distinction has already cost this project one wrong conclusion, when the
corridor probe was answered by the extract.

Every candidate travels in one union per chunk, the same shape `pois.py`
sends, rather than one query per tag. Walking the corridor is what costs, so
twelve queries per chunk would have measured twelve times the wrong thing --
and asked a public server for it. Each returned feature is then classified
locally, which also answers the question a per-tag count cannot: how many of
the cafes and hotels along this route carry a motorcycle tag as well.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _env  # noqa: E402

try:
    import httpx  # noqa: E402
except ModuleNotFoundError:
    _env.require("httpx")

from moto_route import geo  # noqa: E402
from moto_route.config import Settings  # noqa: E402
from moto_route.parsers import parse_route_bytes  # noqa: E402
from moto_route.services import overpass  # noqa: E402

#: (label, key, value) -- a value of None counts any feature carrying the key.
#: The three the app already shows are here as baselines: a candidate is worth
#: a category relative to them, not in the abstract.
CANDIDATES = [
    ("fuel (baseline)", "amenity", "fuel"),
    ("cafe (baseline)", "amenity", "cafe"),
    ("viewpoint (baseline)", "tourism", "viewpoint"),
    ("motorcycle shop", "shop", "motorcycle"),
    ("motorcycle repair", "shop", "motorcycle_repair"),
    # Athens-Volos returned 0 for shop=motorcycle_repair, which is more likely
    # a tagging scheme than an absence: a repair shop is usually mapped as
    # shop=motorcycle carrying service:motorcycle:repair, not under its own
    # value. If this outscores the standalone tag, the category should be
    # built on it instead.
    ("  +service:*:repair", "service:motorcycle:repair", None),
    ("motorcycle parts", "shop", "motorcycle_parts"),
    ("motorcycle parking", "amenity", "motorcycle_parking"),
    ("motorcycle:theme", "motorcycle:theme", None),
    ("motorcycle_friendly", "motorcycle_friendly", None),
    ("hotel", "tourism", "hotel"),
    ("guest house", "tourism", "guest_house"),
    ("campsite", "tourism", "camp_site"),
    ("motel", "tourism", "motel"),
]

#: Tags that would make a stop worth flagging to a rider, if they exist.
MOTORCYCLE_TAGS = ("motorcycle:theme", "motorcycle_friendly")


def matches(tags: dict, key: str, value: str | None) -> bool:
    return key in tags if value is None else tags.get(key) == value


def selectors() -> list[str]:
    """One selector per key, not per candidate.

    The corridor coordinates are repeated verbatim in every statement, so a
    statement each would have sent the same 60 coordinates twelve times -- a
    13 KB query where 5 KB says the same thing. Candidates sharing a key
    collapse into one regex, which is what `pois.py` already does for
    `amenity~"^(fuel|cafe)$"`. A key wanted without a value subsumes any
    valued sibling, so it wins outright.
    """
    keys: dict[str, list[str] | None] = {}
    for _, key, value in CANDIDATES:
        if value is None:
            keys[key] = None
        elif keys.get(key, []) is not None:
            keys.setdefault(key, []).append(value)
    return [f'["{key}"]' if values is None
            else f'["{key}"~"^({"|".join(values)})$"]'
            for key, values in keys.items()]


def build_query(coords, settings: Settings, radius_m: float) -> str:
    """Every candidate in one union, so the corridor is walked once."""
    joined = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in coords)
    around = f"around:{int(radius_m)},{joined}"
    lines = "\n".join(f"  nwr({around}){sel};" for sel in selectors())
    return f"{overpass.query_header(settings)}\n(\n{lines}\n);\nout center {CAP};\n"


#: Generous, because a truncated union would understate every count at once.
#: Reaching it is reported rather than silently believed.
CAP = 20_000


def report(elements, route, radius_m: float, partial: bool) -> None:
    per_100km = 1 / max(route.distance_m / 100_000, 0.01)

    print(f"\n  {'tag':22s} {'found':>7s}   per 100 km")
    for label, key, value in CANDIDATES:
        n = sum(1 for e in elements if matches(e.get("tags", {}), key, value))
        print(f"  {label:22s} {n:7d}   {n * per_100km:9.1f}")

    # The friend's actual question: not "do these tags exist" but "would
    # filtering the cafe and hotel panels by them leave anything on the map".
    print(f"\n  of which carry {' or '.join(MOTORCYCLE_TAGS)}:")
    for label, key, value in CANDIDATES:
        if key in MOTORCYCLE_TAGS:
            continue
        both = [e for e in elements
                if matches(e.get("tags", {}), key, value)
                and any(t in e.get("tags", {}) for t in MOTORCYCLE_TAGS)]
        if both:
            names = ", ".join(sorted({e["tags"].get("name", "unnamed")
                                      for e in both})[:3])
            print(f"  {label:22s} {len(both):7d}   {names}")

    unclassified = [e for e in elements
                    if not any(matches(e.get("tags", {}), k, v)
                               for _, k, v in CANDIDATES)]
    if unclassified:
        # The query and the classifier are built from one list, so this should
        # be empty. Anything here means a returned feature matched no candidate
        # -- a selector wider than the candidate it was written for.
        print(f"\n  {len(unclassified)} feature(s) matched no candidate; the "
              f"query is asking for more than the census counts:")
        for element in unclassified[:5]:
            tags = element.get("tags", {})
            print(f"    {tags.get('name', 'unnamed')}: "
                  f"{dict(list(tags.items())[:3])}")

    if partial:
        print("\n  PARTIAL: a chunk failed, so these are lower bounds.")
    if len(elements) >= CAP:
        print(f"\n  CAP REACHED ({CAP}): every count above is truncated. "
              f"Re-run on a shorter route or a narrower --radius.")


def public_settings(url: str, settings: Settings) -> Settings:
    """Settings that ask `url` and nothing else, treated as the public server.

    Clearing the coverage matters, and cost a Dolomites run to learn.
    `_is_own_server` asks whether coverage is configured *and* the primary
    endpoint is the configured `overpass_url` -- and for a one-endpoint
    Settings the second test is true by construction. On a box with a local
    extract, `_env.load()` fills the coverage from the environment, so the
    census was handed the local 120 s deadline and local concurrency while
    talking to a public server. Greece answered inside 120 s and hid it; the
    Alps, where every valley is mapped as a hotel, did not.

    The query timeout goes up for the same reason. A 900 s client deadline
    buys nothing while the header still tells Overpass to give up at 90 s.
    """
    return Settings(overpass_url=url, overpass_fallback_urls=[],
                    overpass_coverage_files=[], overpass_coverage=None,
                    overpass_concurrency=settings.overpass_concurrency,
                    overpass_timeout_s=300,
                    overpass_public_deadline_s=900.0)


async def census(route, settings: Settings, url: str, radius_m: float,
                 client: httpx.AsyncClient) -> int:
    points = geo.corridor_points(route.all_latlon, radius_m)
    chunks = overpass.chunk_coordinates(points, settings.overpass_max_points)
    single = public_settings(url, settings)

    print(f"\n{route.name}: {route.distance_m / 1000:.0f} km, "
          f"{radius_m:.0f} m corridor, {len(chunks)} chunk(s) via "
          f"{overpass._host(url)}")
    print(f"  {single.deadline_for(single.overpass_endpoints):.0f}s budget, "
          f"{single.concurrency_for(single.overpass_endpoints)} at a time; "
          f"a dense route on a public server takes minutes")

    try:
        # run_chunked de-duplicates by (type, id), so a feature sitting on the
        # seam between two chunks is counted once.
        result = await overpass.run_chunked(chunks, lambda chunk: build_query(
            chunk, single, radius_m), single, client)
    except overpass.OverpassError as exc:
        print(f"\n  failed: {exc}", file=sys.stderr)
        return 1

    elements = result.get("elements", [])
    print(f"  {len(elements)} features returned")
    report(elements, route, radius_m, bool(result.get("partial")))
    return 0


def dry_run(route, settings: Settings, radius_m: float) -> int:
    """Build every query the census would send, and send none of them.

    The same reason `check_services.py` has one: pytest does not import
    `tools/`, so a call into the app with the wrong arguments stays green until
    it is run against a real server, several merges later.
    """
    points = geo.corridor_points(route.all_latlon, radius_m)
    chunks = overpass.chunk_coordinates(points, settings.overpass_max_points)
    queries = [build_query(chunk, settings, radius_m) for chunk in chunks]

    print(f"\n{route.name}: {route.distance_m / 1000:.0f} km, "
          f"{radius_m:.0f} m corridor")
    print(f"  {len(points)} query points -> {len(chunks)} chunk(s) of at most "
          f"{settings.overpass_max_points}")
    print(f"  {len(CANDIDATES)} tags in {len(queries)} quer"
          f"{'y' if len(queries) == 1 else 'ies'}")
    print(f"  query size: {max(len(q) for q in queries) / 1024:.1f} KB "
          f"(largest chunk)")
    print("\nNothing was sent.")
    return 0


async def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", default=None,
                        help="GPX/KML to census "
                             "(default: examples/dolomites_demo.gpx)")
    parser.add_argument("--url", default=None,
                        help="Overpass to ask (default: the first configured "
                             "server that is not your own)")
    parser.add_argument("--radius", type=float, default=1000.0,
                        help="Corridor in metres (default 1000, as for fuel)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the queries and print the plan; send nothing")
    args = parser.parse_args()

    _env.load()
    settings = Settings()

    path = (Path(args.route).expanduser() if args.route
            else repo / "examples" / "dolomites_demo.gpx")
    try:
        route = parse_route_bytes(path.read_bytes(), path.name)
    except OSError as exc:
        print(f"Could not read {path}: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        return dry_run(route, settings, args.radius)

    url = args.url or next(
        (u for u in settings.overpass_endpoints if not overpass._is_local(u)), None)
    if url is None:
        print("No full-data server configured. A local extract holds only the "
              "tags the app already queries, so it cannot answer this.\n"
              "Pass --url https://overpass-api.de/api/interpreter",
              file=sys.stderr)
        return 2

    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent},
                                 follow_redirects=True) as client:
        return await census(route, settings, url, args.radius, client)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
