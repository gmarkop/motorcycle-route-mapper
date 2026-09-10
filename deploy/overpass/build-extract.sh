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
# Usage:
#   deploy/overpass/build-extract.sh [workdir]
#   deploy/overpass/build-extract.sh --only greece,italy [workdir]
#   deploy/overpass/build-extract.sh --sizes [--only greece,italy] [workdir]
#
# --sizes reports what the selected countries would cost to download, and stops.
# Run it before a large build: the download is the obvious cost, but the
# filtering step is the one that can exhaust a small box, and its appetite
# follows the input size, not the handful of megabytes it produces.
#
# --only restricts the build to the named countries. Use it to prove the whole
# pipeline on two small ones before committing to a 17 GB download; re-running
# later with more countries reuses everything already downloaded and filtered,
# and rebuilds the merged extract from all of them.
#
# Needs:  osmium-tool  (sudo apt install osmium-tool), curl, ~40 GB free.

set -euo pipefail

SIZES_ONLY=""
if [ "${1:-}" = "--sizes" ]; then
  SIZES_ONLY=1
  shift
fi

ONLY=""
if [ "${1:-}" = "--only" ]; then
  [ $# -ge 2 ] || { echo "--only needs a comma-separated list of countries" >&2; exit 1; }
  ONLY="$2"
  shift 2
fi

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
  # Hungary and Slovakia are not destinations, they are the way through:
  # Serbia does not border Slovakia, so any Balkans-to-Poland ride crosses
  # Hungary. A country you only transit still needs its closures searched, and
  # is exactly the one likely to be left out of a list written by thinking
  # about where the trip is going.
  europe/hungary
  europe/slovakia
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
# pois.py:    amenity=fuel, cafe, motorcycle_parking
#             tourism=viewpoint, hotel, guest_house, camp_site, motel
#
# The accommodation and parking tags were chosen by tools/tag_census.py over a
# Greek and an Italian route, not by guess. Tags that came back empty on both
# -- motorcycle_friendly, motorcycle:theme, shop=motorcycle and its repair and
# parts variants -- are deliberately not here: keeping a tag nobody maps costs
# disk and import time to serve an empty panel.
#
# THIS LIST AND pois.CATEGORY_TAGS MUST AGREE. A tag queried but not kept here
# returns nothing from the local server, which reads as "none along this route"
# rather than as a missing import. tests/test_docs.py checks that they match.
KEEP=(
  w/highway=construction
  w/construction
  w/access=no
  w/motor_vehicle=no
  w/seasonal=yes
  w/snowplowing=no
  n/barrier
  nwr/amenity=fuel,cafe,motorcycle_parking
  nwr/tourism=viewpoint,hotel,guest_house,camp_site,motel
)

command -v osmium >/dev/null || { echo "osmium not found: sudo apt install osmium-tool" >&2; exit 1; }
mkdir -p "$WORK/raw" "$WORK/filtered" "$WORK/poly"

# Narrow the list if --only was given, and refuse a name that is not in it --
# a typo would otherwise look like a country that simply produced no data,
# which is exactly the kind of silent gap this whole design is trying to avoid.
if [ -n "$ONLY" ]; then
  selected=()
  IFS=',' read -ra wanted <<< "$ONLY"
  for want in "${wanted[@]}"; do
    want="$(echo "$want" | tr -d ' ')"
    [ -z "$want" ] && continue
    found=""
    for path in "${COUNTRIES[@]}"; do
      [ "${path##*/}" = "$want" ] && { selected+=("$path"); found=1; break; }
    done
    [ -n "$found" ] || {
      echo "Unknown country '$want'. Known: $(printf '%s ' "${COUNTRIES[@]##*/}")" >&2
      exit 1
    }
  done
  COUNTRIES=("${selected[@]}")
  echo "==> Building for ${#COUNTRIES[@]} of the configured countries: $ONLY"
fi

# What this build is about to cost, asked of Geofabrik's headers rather than
# guessed. Worth knowing before committing a 1.9 GB box to it: the download is
# the visible cost, but the filtering step is where the memory goes, and that
# scales with a country's size rather than with the few megabytes it leaves.
if [ -n "$SIZES_ONLY" ]; then
  echo "==> Download size of ${#COUNTRIES[@]} extract(s), from Geofabrik"
  total=0
  unknown=0
  for path in "${COUNTRIES[@]}"; do
    name="${path##*/}"
    # `|| bytes=""` matters: under `set -e` with pipefail, one unreachable
    # country would otherwise abort the whole report instead of printing "?".
    bytes=$(curl -fsSIL --max-time 30 "$REGION_BASE/$path-latest.osm.pbf" 2>/dev/null \
            | tr -d '\r' | awk 'tolower($1)=="content-length:"{n=$2} END{print n}') \
            || bytes=""
    if [ -z "$bytes" ]; then
      printf "    %-14s      ? (could not read the header)\n" "$name"
      unknown=$((unknown + 1))
      continue
    fi
    total=$((total + bytes))
    printf "    %-14s %7.2f GB\n" "$name" "$(echo "$bytes/1073741824" | bc -l)"
  done
  # A total that silently omits the countries it could not reach is worse than
  # no total: it reads as the answer.
  if [ "$unknown" -gt 0 ]; then
    printf "    %-14s %7.2f GB for the %d it could reach -- %d unknown, so this is a lower bound\n" \
           "PARTIAL" "$(echo "$total/1073741824" | bc -l)" \
           "$((${#COUNTRIES[@]} - unknown))" "$unknown"
  else
    printf "    %-14s %7.2f GB to download\n" "TOTAL" \
           "$(echo "$total/1073741824" | bc -l)"
  fi
  # The raw files are kept so a later run can add a country without
  # re-downloading, so peak disk is the downloads plus the filtered copies and
  # the merged output. The filtered copies are tiny; the raw ones are not.
  printf "    %-14s %7.2f GB peak disk, roughly (raw kept + filtered + merged)\n" \
         "" "$(echo "$total*1.15/1073741824" | bc -l)"
  echo
  echo "    Disk is the easy constraint. The filtering step holds an index of"
  echo "    the nodes each kept way refers to, and that follows the size of the"
  echo "    country going in, not the few megabytes coming out -- which is why"
  echo "    a big country is worth trying on its own before a full build."
  echo
  echo "    Free here: $(df -h --output=avail "$WORK" | tail -1 | tr -d ' ')"
  echo "    Nothing was downloaded."
  exit 0
fi

echo "==> Downloading ${#COUNTRIES[@]} country extracts into $WORK/raw"
for path in "${COUNTRIES[@]}"; do
  name="${path##*/}"
  target="$WORK/raw/$name.osm.pbf"
  # -C continues a partial download, so an interrupted run resumes rather than
  # starting the 4 GB files again.
  curl -fL -C - -# --retry 3 --retry-delay 5 \
       -o "$target" "$REGION_BASE/$path-latest.osm.pbf"

  # The clipping polygon Geofabrik used to cut this extract. It is the exact
  # shape of what the country file holds, and the app needs it to know when a
  # route has left the data: a bounding box cannot express "Greece and Italy
  # but not Albania", and Albania sits inside any rectangle drawn around them.
  curl -fL -s --retry 3 --retry-delay 5 \
       -o "$WORK/poly/$name.poly" "$REGION_BASE/$path.poly"
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
# Everything filtered so far, not just this run's countries: adding a country
# later should extend the extract rather than replace it with only the new one.
osmium merge --overwrite -o "$WORK/touring-europe.osm.pbf" "$WORK"/filtered/*.osm.pbf
echo "    merged $(ls -1 "$WORK"/filtered/*.osm.pbf | wc -l) countries"

# Overpass imports bzip2-compressed OSM XML, not PBF. The Docker image expects
# to find exactly that at /db/planet.osm.bz2, and will happily accept a PBF
# under that name and then fail to read it -- which looks like an import that
# ran and produced an empty database. Converting here rather than inside the
# container keeps the step visible, checkable, and done by the same osmium that
# built the extract.
echo "==> Converting to the format Overpass imports (bzip2 XML)"
osmium cat --overwrite -o "$WORK/touring-europe.osm.bz2" "$WORK/touring-europe.osm.pbf"
echo "    $(du -h "$WORK/touring-europe.osm.bz2" | cut -f1) of bzip2 XML" 

echo
echo "Done: $WORK/touring-europe.osm.bz2 ($(du -h "$WORK/touring-europe.osm.bz2" | cut -f1)) — import this one"
echo "The raw downloads in $WORK/raw are no longer needed and can be deleted."

# The app needs to know what this database covers, so that a tour outside it
# goes to the public servers instead of being told "no closures found" by a
# database that has simply never heard of the road. The clipping polygons are
# that answer exactly; a bounding box only approximates it, and approximates
# it in the dangerous direction -- the box around Greece and Italy contains
# eight countries whose data is not here.
polys=$(ls -1 "$WORK"/poly/*.poly 2>/dev/null | tr '\n' ',' | sed 's/,$//')
echo "==> Coverage: $(ls -1 "$WORK"/poly/*.poly 2>/dev/null | wc -l) clipping polygons"

echo
echo "Add this to /etc/moto-route.env:"
echo
echo "    MOTO_OVERPASS_URL=http://127.0.0.1:12345/api/interpreter"
echo "    MOTO_OVERPASS_COVERAGE_FILES=$polys"
echo "    # (no concurrency line needed: MOTO_OVERPASS_LOCAL_CONCURRENCY"
echo "    #  already applies 8 to your server and 2 to the public fallback)"
echo
echo "Do NOT also set MOTO_OVERPASS_COVERAGE: the polygons are exact, and a"
echo "box drawn round them would claim countries this database does not hold."
echo
