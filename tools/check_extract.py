"""Ask your own Overpass whether it actually holds every tag the app queries.

The mirror image of `tag_census.py`. The census asks a *public* server "does
this tag exist in the world?"; this asks your *local* server "did my build
actually keep it?" -- and those are the two halves of a confusion that has cost
this project real time more than once. A tag the app queries but the extract
never kept comes back empty, and empty reads as "none along this route".

    python tools/check_extract.py

Run it after every rebuild, before trusting a route. `KEEP` and the app agree
by test (tests/test_docs.py); what no test can check is whether the build you
actually ran, and the import you actually did, put that data on the server that
is actually running.
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

from moto_route.config import Settings  # noqa: E402
from moto_route.services import overpass  # noqa: E402
from moto_route.services.pois import CATEGORY_TAGS  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

#: Hazards are queried as bare keys or key=value; `build-extract.sh` keeps a
#: deliberate superset of them, so these are the ones whose absence would be
#: felt as a closure the app never mentions.
HAZARD_TAGS = [
    ("highway", "construction"),
    ("construction", None),
    ("access", "no"),
    ("motor_vehicle", "no"),
    ("barrier", None),
]


def selector(key: str, value: str | None) -> str:
    return f'["{key}"="{value}"]' if value else f'["{key}"]'


def wanted() -> list[tuple[str, str, str | None]]:
    """Every tag the app can ask its own server for, with what needs it."""
    out: list[tuple[str, str, str | None]] = []
    for category, (key, values) in CATEGORY_TAGS.items():
        out.extend((category, key, value) for value in values)
    out.extend(("closures", key, value) for key, value in HAZARD_TAGS)
    return out


def bbox(settings: Settings) -> tuple[float, float, float, float]:
    """A box around everything the extract claims to hold.

    Counting over the whole coverage is the point: a tag present in Italy and
    missing in Greece is still a hole, and a route-shaped probe would only find
    it by accident.
    """
    area = settings.coverage_area
    if area is not None and area.rings:
        outer = [ring for ring in area.rings if not ring.hole] or list(area.rings)
        return (min(r.south for r in outer), min(r.west for r in outer),
                max(r.north for r in outer), max(r.east for r in outer))
    if settings.overpass_coverage:
        return settings.overpass_coverage
    raise SystemExit(
        "No coverage configured, so there is no area to count over.\n"
        "Set MOTO_OVERPASS_COVERAGE_FILES (preferred) or MOTO_OVERPASS_COVERAGE.")


async def count(client: httpx.AsyncClient, url: str, settings: Settings,
                key: str, value: str | None,
                box: tuple[float, float, float, float]) -> int | None:
    """How many features carry this tag, over the whole extract.

    `out count` returns a total without shipping the features, which is what
    makes counting an entire country cheap enough to do for every tag.
    """
    query = (f"{overpass.query_header(settings)}"
             f"nwr({box[0]},{box[1]},{box[2]},{box[3]}){selector(key, value)};"
             f"out count;")
    try:
        data = await overpass.run_query(query, settings, client)
    except overpass.OverpassError as exc:
        print(f"  {RED}query failed{RESET}: {exc}", file=sys.stderr)
        return None
    for element in data.get("elements", []):
        if element.get("type") == "count":
            return int(element.get("tags", {}).get("total", 0))
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None,
                        help="Overpass to check (default: your configured primary)")
    parser.add_argument("--allow-public", action="store_true",
                        help="Check a public server anyway. Almost never what "
                             "you want: a public server holds everything, so "
                             "it can only tell you the tags are real")
    args = parser.parse_args()

    _env.load()
    settings = Settings()
    url = args.url or settings.overpass_url

    if not overpass._is_local(url) and not args.allow_public:
        print(f"{url} is not your own server, and a public one holds every tag "
              f"by definition\nso it cannot tell you whether YOUR build kept "
              f"them. Point --url at your instance,\nor pass --allow-public if "
              f"you meant this.", file=sys.stderr)
        return 2

    box = bbox(settings)
    print(f"\nChecking {overpass._host(url)} over "
          f"{box[0]:.1f},{box[1]:.1f} to {box[2]:.1f},{box[3]:.1f}")
    print(f"{DIM}  every tag the app queries, counted across the whole "
          f"extract{RESET}\n")

    empty: list[str] = []
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent},
                                 follow_redirects=True) as client:
        single = Settings(overpass_url=url, overpass_fallback_urls=[],
                          overpass_coverage_files=[], overpass_coverage=None,
                          overpass_timeout_s=180)
        for needed_by, key, value in wanted():
            total = await count(client, url, single, key, value, box)
            tag = f"{key}={value}" if value else key
            if total is None:
                mark, note = f"{RED}  ??{RESET}", "could not be counted"
            elif total == 0:
                mark, note = f"{RED}FAIL{RESET}", "NOT IN THIS EXTRACT"
                empty.append(tag)
            else:
                mark, note = f"{GREEN}  ok{RESET}", f"{total:,}"
            print(f"  {mark}  {tag:34s} {note:22s} {DIM}{needed_by}{RESET}")

    if empty:
        print(f"\n{RED}{len(empty)} tag(s) the app asks for are not in the "
              f"extract:{RESET} {', '.join(empty)}")
        print("  Routes will show these as empty, which looks identical to "
              "'none along this route'.")
        print("  Either KEEP did not have them when you built, or the import "
              "did not run. Rebuild,\n  re-import, and check again.")
        return 1

    print(f"\n{GREEN}Every tag the app queries is present.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
