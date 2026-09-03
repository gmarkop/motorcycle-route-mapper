# Motorcycle Route Mapper — working notes

Self-hosted tour planner for European motorcycle trips. Loads a GPX/KML/KMZ
route, draws it, and layers live conditions on top. Runs on the owner's own
machine; no accounts, no API keys, no subscription.

Owner: gmarkop. Repo: `gmarkop/motorcycle-route-mapper` (**private**).

---

## Where things stand (3 September 2026)

`main` carries everything: the original app, the five touring features, and now
offline route persistence. The old `claude/touring-features` branch is merged
and deleted, as is the abandoned branch that once lived in the ELAN repo.

### Open admin items

- The Autobahn incident provider has **never been run against the live API** —
  see "Unverified" below.
- Branch deletion cannot be done from this environment: the git proxy answers
  403 to delete refspecs and the GitHub tools have `create_branch` but no
  `delete_branch`. The owner deletes branches through the GitHub UI.

---

## Next up

### Done: client-side route persistence

Shipped. `static/js/store.js` keeps the parsed route, the last good response
from every live layer, and the original file's bytes in IndexedDB, capped at
five rides. Reloading offline restores the ride with a banner saying what is
live and what is saved; a forgotten server route id (the in-memory store dies
with the process) is recovered by silently re-uploading the kept file.

### Done: deployment

Shipped. `DEPLOY.md` is the guide; `deploy/` holds a hardened systemd unit, an
idempotent `install.sh` that installs from the local checkout (so a private repo
needs no credentials on the server), and a commented env template.

The service binds to `127.0.0.1` only and runs as an unprivileged `motoroute`
user under `ProtectSystem=strict`, an empty capability set and a syscall filter.
Tailscale is the recommended way in: it gives a real certificate (so the tile
service worker registers) and keeps an app with no authentication off the public
internet.

### Nothing agreed for next time

Open ideas, in rough order of value: verifying the Autobahn provider against
the live API; more incident providers; a `MOTO_TILE_URL`
setting (the tile server is currently hard-coded in `mapview.js`, and the docs
had to be corrected to say so); multi-day tours; rider-tuned rideability weights.

---

## Running and testing

```bash
python run.py                 # http://127.0.0.1:8000
python run.py --offline       # map and route only, no network calls
python -m pytest              # 173 tests, fully offline, ~10 s
python -m pyflakes moto_route/ tests/ run.py
```

Python 3.11+. Deps in `requirements.txt`.

### Verifying against live-shaped data without the network

`tools/stub_apis.py` impersonates Open-Meteo, Overpass, OSRM and the Autobahn
API. It is how every feature was actually verified, since this build environment
has no outbound access:

```bash
python -m uvicorn tools.stub_apis:app --port 8940 &
MOTO_WEATHER_URL=http://127.0.0.1:8940/v1/forecast \
MOTO_OVERPASS_URL=http://127.0.0.1:8940/api/interpreter \
MOTO_OSRM_URL=http://127.0.0.1:8940 \
MOTO_AUTOBAHN_URL=http://127.0.0.1:8940/o/autobahn \
python run.py --no-browser
```

Then drive the real UI with `tools/browser_test.py`, which loads a ride, turns
the network genuinely off, reloads, and checks the ride comes back:

```bash
python tools/browser_test.py --url http://127.0.0.1:8961/
```

**Browser verification is not optional here.** Every bug in the list below was
invisible to `pytest` and only showed up in a live page.

For the deployment side, `systemd-analyze verify deploy/moto-route.service`
checks the unit, and `tests/test_docs.py` fails if any document names a `MOTO_*`
setting that `config.py` does not define — it caught an invented one.

---

## Architecture

Parse once, enrich independently. A file becomes a `Route` held in memory under
an id; the browser then requests weather, POIs, hazards, incidents and
alternates as five parallel, independent calls. One slow service never blocks
the map.

```
moto_route/
├── geo.py          Distances, bearings, simplification, sampling, curviness
├── models.py       GeoPoint / Waypoint / Route — the one shape everything speaks
├── config.py       Settings, all from environment variables
├── export.py       Enriched GPX writer
├── api.py          Endpoints + the in-memory RouteStore
├── parsers/        gpx.py, kml.py, common.py (namespace-agnostic XML + bomb guard)
├── services/       weather, hazards, pois, incidents, alternates, cache
└── static/
    ├── sw.js       Service worker: app shell + tile caching
    └── js/         Native ES modules: app, mapview, panels, tiles, format
```

No bundler, no build step, no frontend framework. Leaflet is vendored in
`static/vendor/leaflet/` so the UI works with no internet.

---

## Conventions worth keeping

- **Services fail soft.** Every enrichment endpoint returns
  `{"available": false, "reason": "..."}` rather than an HTTP error when a
  source is down. A dead layer is one sentence in the sidebar, never a broken
  page. Keep this for any new provider.
- **Comments explain *why*, never *what*.** The existing density is the target —
  match it. Anything non-obvious (a coordinate-order trap, a policy limit, a
  falsy-zero hazard) earns a comment; a loop that reads plainly does not.
- **Judgement calls are tested as relationships, not fixed numbers.** The
  rideability score and the fuel planner are opinions. Tests assert that
  freezing rain always scores worse than a merely wet day, and that the planner
  always takes the furthest reachable pump — not that a specific input yields 62.
  Retuning a weight must not turn the suite red.
- **HTTP is mocked at the transport** (`httpx.MockTransport`), never by patching
  the service function. The real request-building, status handling and JSON
  parsing all execute; only the socket is fake.
- **Respect the free services.** Overpass corridor queries, not bounding boxes.
  Batched Open-Meteo calls. Cached responses with sane TTLs. A `User-Agent` that
  identifies the app. Tile prefetch capped at 250 and throttled, per OSM's usage
  policy. These are the reason the app has no subscription; do not erode them.

---

## Gotchas already paid for

Every one of these was a real bug found during verification. Do not reintroduce.

- **`0` is falsy.** WMO weather code 0 means "clear sky" — the best weather
  there is. `CODES.get(code or -1)` silently turns it into "Unknown". There is a
  regression test named after this.
- **Coordinate order flips by format.** GPX uses `lat`/`lon` attributes; KML and
  GeoJSON both use `longitude, latitude` in text. Getting it wrong puts a
  Bavarian ride in Somalia. The Autobahn API spells it `long`, not `lon`.
- **A "blank" 1×1 PNG from the internet is often not transparent.** The one
  originally used was RGBA(0,255,0,127) and tiled the whole viewport green.
  Decode the alpha channel before trusting any such constant.
- **A service worker's scope is derived from its own path.** Served from
  `/static/sw.js` it can never control the page. It is served from `/sw.js` via
  an explicit route in `api.py` — keep it there.
- **Draw casings in a separate pass from strokes.** Casing-then-colour per
  segment lets each segment's casing paint over the previous segment's colour;
  on any road that doubles back, the whole line goes black.
- **`Math.floor` of a tiny negative is -1, not 0.** Web Mercator at the poles
  produces exactly that, yielding an impossible tile index. Tile indices are
  clamped into `[0, 2^z)`.
- **A range input clamps assignment silently.** Setting `.value` below its `min`
  leaves the slider and its label disagreeing. Read the value back after setting.
- **Douglas-Peucker is O(n²) on noisy input.** A radial pre-filter plus an
  explicit evaluation budget keeps a pathological 20k-point track under two
  seconds; the budget degrades resolution rather than failing.
- **Overpass allows about two concurrent queries per IP.** This app asks it
  three questions (closures, POIs, motorway refs) and the browser fires the
  layers in parallel, so the third used to get a 429 and the rider saw
  "unavailable (HTTPStatusError)". Everything now goes through
  `services/overpass.py`, which holds a semaphore (`MOTO_OVERPASS_CONCURRENCY`,
  default 1), retries transient statuses honouring `Retry-After`, and turns
  status codes into sentences. Do not add a fourth caller that bypasses it.
- **A declared query budget and the HTTP timeout must come from one number.**
  The POI query told Overpass it could take 90 seconds while the shared client
  hung up after 20, so every genuinely slow query failed client-side and was
  reported as a connection timeout. `overpass.query_header()` and the per-request
  timeout now both derive from `MOTO_OVERPASS_TIMEOUT`, and a test asserts the
  HTTP wait always outlasts the declared budget.
- **"Unnamed route point" does not always mean "shaping point".** Garmin names
  its stops and leaves shaping points bare, so the rule holds there. Generic
  converters name nothing, and applying the rule marked every point as scenery —
  the rider's waypoints vanished into 3px grey dots. The count decides instead:
  at most `MAX_IMPLICIT_VIA_POINTS` unnamed points are the rider's stops, more
  than that is an exported driving polyline. Both mistakes are equally bad, in
  opposite directions.
- **Never put a status code in an exception name and call it a message.**
  `f"unavailable ({type(exc).__name__})"` is undiagnosable; the status is the
  one fact that matters.
- **National feeds need a coverage check.** Greek and Austrian motorways use the
  same `A1`, `A2` numbering as German ones, so the Autobahn provider happily
  "detected" Greek roads and queried the German API about them. Providers now
  answer `covers(route)`; the Autobahn one checks a Germany bounding box.
- **IndexedDB read-modify-write must happen in ONE transaction.** Five layers
  save concurrently; a `get` in one transaction followed by a `put` in another
  loses updates, because each reads before the others write. The symptom was
  three of seven layers persisting. `store.js` issues the `put` from inside the
  `get` callback so the pair is atomic.
- **`StartLimitIntervalSec` and `StartLimitBurst` live in `[Unit]`.** systemd
  silently ignores them under `[Service]`, so a misplaced pair looks fine and
  does nothing. `systemd-analyze verify` catches it; run it after any unit edit.
- **Parsing an ini file by splitting on `"[Section]"` is wrong** when the file's
  own comments mention section names. `tests/test_docs.py` walks lines instead.
- **A hidden panel still holds its old DOM.** Hiding the saved-rides list
  without clearing it left rows for rides already deleted.
- **`xml.etree` expands entity declarations.** Route files never need a DTD, so
  `parsers/common.py` refuses one outright. Keep that guard on any new parser.

---

## Unverified

The **Autobahn incident provider** (`services/incidents.py`) was written against
the documented shape of Germany's Autobahn GmbH API and is covered by tests
using recorded fixtures, but the live endpoints are unreachable from this build
environment (the egress proxy answers `connect_rejected`), so it has never seen
real data.

It fails soft, so a wrong guess about the schema shows an empty layer rather
than breaking anything. **Do not present it as working** until someone has
watched it return a real incident.

`tools/verify_autobahn.py` settles it from any machine with a connection. It
checks the payload key per service, the `long`-not-`lon` coordinate spelling,
whether `description` is a list, and the OpenStreetMap motorway detection — then
runs the provider's own `_item_to_incident` over the live payloads, so the check
cannot drift from the implementation. It also sanity-checks that mapped
coordinates land inside Germany, which is what would catch a silent latitude
/longitude swap. Exit 0 means the provider matches the live API.

The script honours `MOTO_AUTOBAHN_URL`, so it can be pointed at
`tools/stub_apis.py` as a self-test; the Germany bounds check is skipped when
the endpoint is not the public API.
