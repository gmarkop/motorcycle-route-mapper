"""Check what a self-hosted Overpass is trusted to answer for.

The coverage polygons decide, per route, whether the closure and points-of-
interest layers ask your own server or the public ones. Get them wrong in the
generous direction and the app reports "no closures" for a country your
database has never heard of — a wrong answer that looks exactly like a right
one, on the layer where that matters most.

So check them before you rely on them:

    python tools/check_coverage.py                      # reads /etc/moto-route.env
    python tools/check_coverage.py greece.poly italy.poly
    python tools/check_coverage.py --route ~/tour.gpx   # your own ride

With no route it tests a list of European cities and prints which side of the
boundary each falls on, so a polygon that quietly failed to download, or a
country you forgot to include, shows up as a city in the wrong column.

Exit code 0 means the polygons loaded and every named route point is covered.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moto_route import coverage as coverage_mod  # noqa: E402

#: Places worth knowing the answer for. The Balkan entries are the ones that
#: matter: they sit inside any rectangle drawn around Greece and Italy, and are
#: what an overland ride between the two actually crosses.
PROBES = [
    ("Athens", 37.98, 23.73),
    ("Thessaloniki", 40.64, 22.94),
    ("Heraklion, Crete", 35.34, 25.13),
    ("Rhodes", 36.43, 28.22),
    ("Milan", 45.46, 9.19),
    ("Rome", 41.90, 12.50),
    ("Palermo, Sicily", 38.12, 13.36),
    ("Bolzano", 46.50, 11.35),
    ("Tirana, Albania", 41.33, 19.82),
    ("Podgorica, Montenegro", 42.44, 19.26),
    ("Sarajevo, Bosnia", 43.86, 18.41),
    ("Zagreb, Croatia", 45.81, 15.98),
    ("Ljubljana, Slovenia", 46.06, 14.51),
    ("Sofia, Bulgaria", 42.70, 23.32),
    ("Belgrade, Serbia", 44.79, 20.45),
    ("Vienna, Austria", 48.21, 16.37),
    ("Munich, Germany", 48.14, 11.58),
    ("Istanbul, Turkey", 41.01, 28.98),
    ("Tunis, Tunisia", 36.81, 10.17),
]

ENV_FILE = "/etc/moto-route.env"


def paths_from_env() -> list[str]:
    """MOTO_OVERPASS_COVERAGE_FILES, from the environment or the unit's env file."""
    raw = os.environ.get("MOTO_OVERPASS_COVERAGE_FILES", "")
    if not raw and Path(ENV_FILE).is_file():
        for line in Path(ENV_FILE).read_text().splitlines():
            line = line.strip()
            if line.startswith("MOTO_OVERPASS_COVERAGE_FILES="):
                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
    return [p.strip() for p in raw.split(",") if p.strip()]


def route_points(path: Path) -> list[tuple[float, float]]:
    from moto_route.parsers import parse_route_bytes
    route = parse_route_bytes(path.read_bytes(), path.name)
    return route.all_latlon


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("polys", nargs="*", help="`.poly` files (default: from settings)")
    parser.add_argument("--route", type=Path, help="GPX/KML to test point by point")
    args = parser.parse_args()

    paths = args.polys or paths_from_env()
    if not paths:
        print("No coverage files given and none in the environment or "
              f"{ENV_FILE}.\nPass them as arguments, or set "
              "MOTO_OVERPASS_COVERAGE_FILES.", file=sys.stderr)
        return 2

    print("Coverage files:")
    for path in paths:
        exists = Path(path).is_file()
        size = Path(path).stat().st_size if exists else 0
        print(f"  {'OK ' if exists and size else 'MISSING'} {path} ({size} bytes)")
    if not all(Path(p).is_file() and Path(p).stat().st_size for p in paths):
        return 1

    try:
        area = coverage_mod.load(paths)
    except coverage_mod.CoverageError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1

    solid = sum(1 for r in area.rings if not r.hole)
    holes = len(area.rings) - solid
    print(f"\nParsed {len(area.rings)} rings ({solid} areas, {holes} holes).")
    if solid == 0:
        print("FAIL: no solid rings — nothing would ever be covered.", file=sys.stderr)
        return 1

    print("\nWhere the local server will be trusted:\n")
    print(f"  {'place':26s} {'verdict'}")
    for name, lat, lon in PROBES:
        inside = area.contains(lat, lon)
        print(f"  {name:26s} {'LOCAL  (your server)' if inside else 'public (fallback)'}")

    failures = 0
    if args.route:
        points = route_points(args.route)
        outside = [(lat, lon) for lat, lon in points if not area.contains(lat, lon)]
        print(f"\nRoute {args.route.name}: {len(points)} points, "
              f"{len(outside)} outside the coverage.")
        if outside:
            lat, lon = outside[0]
            print(f"  First uncovered point: {lat:.4f},{lon:.4f}")
            print("  This route will use the PUBLIC servers — correctly, since "
                  "your database does not hold all of it.")
            failures = 1
        else:
            print("  Fully covered: this route will use your own server.")

    print("\nA place in the wrong column means the polygons do not match the "
          "countries you\nbuilt. Rebuild with build-extract.sh rather than "
          "editing the boxes by hand.")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
