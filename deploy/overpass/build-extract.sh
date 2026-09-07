#!/usr/bin/env bash
# Build a small OSM extract holding only what this app asks Overpass for.
#
# The point is RAM. A conventional Overpass instance covering a dozen European
# countries wants 8-16 GB; a 2 GB box cannot serve one, and can barely import
# one. But this app queries a handful of tags, not the map: fuel, cafes,
# viewpoints, and the roads and barriers that mark a closure. Everything else
# in the extract -- buildings, landuse, addresses, the whole of the rest of
# OpenStreetMap -- is imported, indexed and never once read.
#
# So each country is filtered down to those tags before import. The result is
# a fraction of the input, small enough that the finished database fits in
# page cache on a modest machine, which is where Overpass's speed comes from.
#
# The cost of the trick: this database can answer THIS app's questions and no
# others. Add a layer that needs a new tag and the extract must be rebuilt with
# that tag added to KEEP below. That is the trade, and it is written down here
# so the next person does not discover it by getting empty results.
#
# Usage:  deploy/overpass/build-extract.sh [workdir]
# Needs:  osmium-tool  (sudo apt install osmium-tool), curl, ~40 GB free.

set -euo pipefail

WORK="${1:-/var/lib/overpass-build}"
REGION_BASE="https://download.geofabrik.de"

# Country extracts, as Geofabrik paths without the "-latest.osm.pbf".
COUNTRIES=(
  europe/greece
  europe/italy
  europe/macedonia
  europe/serbia
  europe/montenegro
  europe/bulgaria
  europe/romania
  europe/poland
  europe/austria
  europe/germany
  europe/switzerland
  europe/belgium
  europe/liechtenstein
  europe/luxembourg
  europe/france
)

# Every tag this app's Overpass queries can match. Kept deliberately wider than
# the queries themselves: osmium filters are OR'd and cannot express "highway
# AND access=no", so we keep all access=no ways and let Overpass do the AND at
# query time. A superset costs a little disk and cannot cause a wrong answer.
#
# hazards.py: highway=construction, construction, access=no, motor_vehicle=no,
#             seasonal=yes, snowplowing=no, and barrier nodes
# pois.py:    amenity=fuel, amenity=cafe, tourism=viewpoint
KEEP=(
  w/highway=construction
  w/construction
  w/access=no
  w/motor_vehicle=no
  w/seasonal=yes
  w/snowplowing=no
  n/barrier
  nwr/amenity=fuel,cafe
  nwr/tourism=viewpoint
)

command -v osmium >/dev/null || { echo "osmium not found: sudo apt install osmium-tool" >&2; exit 1; }
mkdir -p "$WORK/raw" "$WORK/filtered"

echo "==> Downloading ${#COUNTRIES[@]} country extracts into $WORK/raw"
for path in "${COUNTRIES[@]}"; do
  name="${path##*/}"
  target="$WORK/raw/$name.osm.pbf"
  # -C continues a partial download, so an interrupted run resumes rather than
  # starting the 4 GB files again.
  curl -fL -C - -# --retry 3 --retry-delay 5 \
       -o "$target" "$REGION_BASE/$path-latest.osm.pbf"
done

echo "==> Filtering each country down to the tags this app queries"
for path in "${COUNTRIES[@]}"; do
  name="${path##*/}"
  src="$WORK/raw/$name.osm.pbf"
  out="$WORK/filtered/$name.osm.pbf"
  [ -f "$out" ] && { echo "    $name: already filtered"; continue; }

  # No -R here, deliberately. osmium's -R is --omit-referenced: it *drops* the
  # nodes a kept way points at. The default keeps them, which is what we need
  # -- a way without its nodes has no geometry, and the closure query asks for
  # "out geom". Verified on a hand-built sample: a construction way comes
  # through with all three of its nodes, a building way and a bench node do
  # not.
  osmium tags-filter --overwrite -o "$out" "$src" "${KEEP[@]}"

  before=$(du -m "$src" | cut -f1)
  after=$(du -m "$out" | cut -f1)
  printf "    %-14s %6s MB -> %5s MB\n" "$name" "$before" "$after"
done

echo "==> Merging into one extract"
osmium merge --overwrite -o "$WORK/touring-europe.osm.pbf" "$WORK"/filtered/*.osm.pbf

echo
echo "Done: $WORK/touring-europe.osm.pbf ($(du -h "$WORK/touring-europe.osm.pbf" | cut -f1))"
echo "The raw downloads in $WORK/raw are no longer needed and can be deleted."

# The app needs to know what this database covers, so that a tour outside it
# goes to the public servers instead of being told "no closures found" by a
# database that has simply never heard of the road.
#
# osmium reports (west,south,east,north); MOTO_OVERPASS_COVERAGE wants
# (south,west,north,east). Reordering it here beats leaving a trap that shows
# up months later as a coverage box silently rotated ninety degrees.
echo "==> Computing the coverage box (reads the whole file; takes a minute)"
bbox=$(osmium fileinfo -e -g data.bbox "$WORK/touring-europe.osm.pbf" | tr -d '()')
west=$(echo "$bbox" | cut -d, -f1)
south=$(echo "$bbox" | cut -d, -f2)
east=$(echo "$bbox" | cut -d, -f3)
north=$(echo "$bbox" | cut -d, -f4)

echo
echo "Add this to /etc/moto-route.env:"
echo
echo "    MOTO_OVERPASS_URL=http://127.0.0.1:12345/api/interpreter"
echo "    MOTO_OVERPASS_COVERAGE=$south,$west,$north,$east"
echo "    MOTO_OVERPASS_CONCURRENCY=4"
echo
