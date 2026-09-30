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


def odd_against_its_peers(total: int, peak: int) -> bool:
    """Is this count out of line with the healthiest tag sharing its key?

    A question, not a verdict, which is the correction. Every tag of the
    import that actually failed here came back under a hundred across two whole
    countries -- 17 hotels, 23 guest houses, 2 camp sites -- so the floor caught
    all of them and this rule caught none. What it did catch was
    `tourism=motel` at 266 against 32,727 hotels, which is simply true: motels
    are a North American idea and southern Europe has few.

    So it now reports rather than fails. It still earns its place: a partial
    import can leave a tag above the floor and far below its peers, and nothing
    else would notice. But being outnumbered is a reason to look, and
    --second-opinion is how to settle it.
    """
    return peak > 0 and total * 100 < peak


async def sample_box(client, url: str, settings: Settings,
                     box: tuple[float, float, float, float],
                     biggest: dict) -> tuple[float, float, float, float] | None:
    """A small area inside the coverage with enough in it to be worth counting.

    Counting a whole country on a public server is a rude question and a slow
    one -- the first version of this asked for every hotel between Tunisia and
    Ukraine, and hung. A degree-and-a-half box answers in seconds and the
    *share* of one tag against another is what the comparison needs anyway.

    Found by asking the local server, which is free, and keeping the first
    candidate that actually holds something -- so the box is guaranteed to be
    inside the coverage rather than out at sea or over a neighbour.
    """
    if not biggest:
        return None
    key, (_, value) = max(biggest.items(), key=lambda kv: kv[1][0])
    south, west, north, east = box
    span = 1.5
    # Walk in from the corners of the coverage towards its middle: the middle
    # of a bounding box around Greece and Italy is the Adriatic.
    for fy in (0.5, 0.25, 0.75, 0.4, 0.6):
        for fx in (0.5, 0.25, 0.75, 0.4, 0.6):
            lat = south + (north - south) * fy
            lon = west + (east - west) * fx
            candidate = (lat, lon, min(lat + span, north), min(lon + span, east))
            found = await count(client, url, settings, key, value, candidate)
            if found and found > 200:
                return candidate
    return None


def merely_rare(here: float, there: float) -> bool:
    """Given both densities, is this tag rare or was it never extracted?

    Compared as a share of the healthiest tag on the same key, because raw
    counts cannot be: the coverage box reaches well past the countries in the
    extract, so a public server legitimately returns far more of everything.
    Shares survive that.

    A factor of ten is loose on purpose. Regions genuinely differ -- motels are
    scarcer in Greece than across the whole box -- and the gap this has to
    catch is not a factor of ten but of thousands: 0.00095 hotels per viewpoint
    in a broken extract against 3.4 in a real one.
    """
    return here * 10 >= there


def implausible(total: int, peak: int, floor: int) -> bool:
    """Is this count too small to have come from a filter that asked for it?

    Two rules, because neither alone is enough. The ratio catches what the
    absolute floor cannot: tags sharing a key sit within an order of magnitude
    of each other in real data, so `tourism=viewpoint` outnumbering
    `tourism=hotel` a thousand to one across Greece and Italy is a fact about
    the build, not about tourism. The floor catches the case the ratio cannot,
    where every value of a key is missing together and there is no healthy peer
    to compare against.
    """
    return total < floor or (peak > 0 and total * 100 < peak)


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
    parser.add_argument("--floor", type=int, default=100,
                        help="Below this many features across the whole "
                             "extract, a tag is reported as implausible "
                             "(default 100). A heuristic, and the reason the "
                             "ratio test above it exists")
    parser.add_argument("--second-opinion", action="store_true",
                        help="Settle any tag reported as odd by asking a public "
                             "server about a sample area. Off by default: it "
                             "sends real queries to somebody else's machine")
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

    counts: dict[tuple[str, str, str | None], int | None] = {}
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent},
                                 follow_redirects=True) as client:
        single = Settings(overpass_url=url, overpass_fallback_urls=[],
                          overpass_coverage_files=[], overpass_coverage=None,
                          overpass_timeout_s=180)
        for needed_by, key, value in wanted():
            counts[(needed_by, key, value)] = await count(
                client, url, single, key, value, box)

    # Zero is not the only way an import fails, and was not how this one did.
    # A tag left out of the filter still returns a scattering of features --
    # the ones dragged in as members of relations that *were* kept -- so a
    # whole-country extract reported 17 hotels and passed. The tell is the
    # ratio: tags sharing a key sit within an order of magnitude of each other
    # in real data, and `tourism=viewpoint` outnumbering `tourism=hotel` by a
    # thousand to one across Greece and Italy is not a fact about tourism.
    biggest: dict[str, tuple[int, str | None]] = {}
    for (_, key, value), total in counts.items():
        if total and total > biggest.get(key, (0, None))[0]:
            biggest[key] = (total, value)

    failed, suspect = [], []
    for (needed_by, key, value), total in counts.items():
        tag = f"{key}={value}" if value else key
        peak = biggest.get(key, (0, None))[0]
        if total is None:
            mark, note = f"{RED}  ??{RESET}", "could not be counted"
        elif total == 0:
            mark, note = f"{RED}FAIL{RESET}", "NOT IN THIS EXTRACT"
            failed.append(tag)
        elif total < args.floor:
            mark, note = f"{RED}FAIL{RESET}", f"only {total:,} — far too few"
            failed.append(tag)
        elif odd_against_its_peers(total, peak):
            mark = f"{YELLOW}  ?{RESET}"
            note = f"{total:,} — vs {peak:,} for {key}"
            suspect.append(tag)
        else:
            mark, note = f"{GREEN}  ok{RESET}", f"{total:,}"
        print(f"  {mark}  {tag:34s} {note:30s} {DIM}{needed_by}{RESET}")

    # A ratio is a screen, not a verdict. `tourism=motel` at 266 against 32,727
    # hotels trips it, and is simply true: motels are a North American idea and
    # Greece and Italy have few, which is what the census found when it counted
    # zero along both routes. So ask a server that holds everything.
    #
    # Raw counts cannot be compared -- the coverage box reaches well past the
    # countries in the extract, so a public server legitimately returns far
    # more. What compares is the shape: motels *per hotel* here, against motels
    # per hotel there. A tag that was never filtered for is orders of magnitude
    # out; a tag that is merely rare matches.
    if suspect and args.second_opinion:
        public = next((u for u in settings.overpass_endpoints
                       if not overpass._is_local(u)), None)
        if public is None:
            print(f"\n{DIM}  No public server configured, so the counts above "
                  f"cannot be checked against one.{RESET}")
        else:
            elsewhere = Settings(overpass_url=public, overpass_fallback_urls=[],
                                 overpass_coverage_files=[],
                                 overpass_coverage=None, overpass_timeout_s=180)
            async with httpx.AsyncClient(
                    headers={"User-Agent": settings.user_agent},
                    follow_redirects=True, timeout=200.0) as client:
                sample = await sample_box(client, url, single, box, biggest)
                if sample is None:
                    print(f"\n{DIM}  No populated sample area found, so there "
                          f"is nothing cheap to compare.{RESET}")
                    suspect_resolved = False
                else:
                    print(f"\n  asking {overpass._host(public)} about "
                          f"{sample[0]:.1f},{sample[1]:.1f} to "
                          f"{sample[2]:.1f},{sample[3]:.1f} — a sample, because "
                          f"the whole\n  coverage is far too big a question to "
                          f"put to someone else's server")
                    box = sample
                    for (needed_by, key, value) in list(counts):
                        counts[(needed_by, key, value)] = await count(
                            client, url, single, key, value, box)
                    biggest = {}
                    for (_, key, value), total in counts.items():
                        if total and total > biggest.get(key, (0, None))[0]:
                            biggest[key] = (total, value)
                for tag in list(suspect) if sample else []:
                    key, _, value = tag.partition("=")
                    value = value or None
                    peak_total, peak_value = biggest.get(key, (0, None))
                    mine = counts[next(k for k in counts
                                       if k[1] == key and k[2] == value)]
                    theirs = await count(client, public, elsewhere, key, value, box)
                    theirs_peak = await count(client, public, elsewhere, key,
                                              peak_value, box)
                    if not theirs or not theirs_peak or not peak_total:
                        print(f"  {DIM}  {tag}: no answer, leaving it flagged"
                              f"{RESET}")
                        continue
                    here = mine / peak_total
                    there = theirs / theirs_peak
                    if merely_rare(here, there):
                        suspect.remove(tag)
                        print(f"  {GREEN}  ok{RESET}  {tag:30s} rare, not "
                              f"missing: {here:.4f} per {key}={peak_value} "
                              f"here, {there:.4f} there")
                    else:
                        print(f"  {RED}FAIL{RESET}  {tag:30s} missing: "
                              f"{here:.5f} per {key}={peak_value} here, "
                              f"{there:.4f} there")
                        # Promoted, not duplicated: it is no longer a suspicion.
                        suspect.remove(tag)
                        failed.append(tag)

    if failed:
        print(f"\n{RED}{len(failed)} tag(s) are missing from the extract:"
              f"{RESET} {', '.join(failed)}")
        print("  A tag left out of the filter still arrives in small numbers, "
              "as members of\n  relations that were kept, so a count in the "
              "tens across two countries is not a\n  smaller version of "
              "working -- the tag was never extracted.")
        print("\n  Most likely the filtered country files predate the change "
              "to KEEP. Rebuild\n  (build-extract.sh re-filters when KEEP "
              "changes), re-import, restart, re-check.")
        return 1

    if suspect:
        print(f"\n{YELLOW}Worth a look:{RESET} {', '.join(suspect)} "
              f"{'is' if len(suspect) == 1 else 'are'} present, but far "
              f"outnumbered by others sharing the same key.")
        print("  Usually that is true rather than broken -- tourism=motel is a "
              "North American\n  idea and southern Europe has few. "
              "--second-opinion settles it against a public\n  server. Not a "
              "failure on its own: everything the app queries is here.")

    print(f"\n{GREEN}Every tag the app queries is present, in usable "
          f"numbers.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
