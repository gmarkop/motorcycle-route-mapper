# Motorcycle Route Mapper — working notes

Self-hosted tour planner for European motorcycle trips. Loads a GPX/KML/KMZ
route, draws it, and layers live conditions on top. Runs on the owner's own
machine; no accounts, no API keys, no subscription.

Owner: gmarkop. Repo: `gmarkop/motorcycle-route-mapper` (**private**).

---

## Where things stand (2 September 2026)

| Branch | Commit | State |
| --- | --- | --- |
| `main` | `3942dc3` | Original app: parsers, weather, hazards, alternates, elevation |
| `claude/touring-features` | `5e647b1` | **Unmerged.** Fuel planning, curviness heat map, GPX export, offline tiles, incidents |

`claude/touring-features` is reviewed-by-nobody but fully tested and verified in
a real browser. **Decide whether to merge it into `main` before building on
top** — the next two tasks both assume it is present.

### Open admin items

- No `LICENSE` file. Private repo, so nothing is broken, but "public" would
  legally mean look-don't-touch without one.
- The abandoned branch `claude/motorcycle-route-mapper-e16cgn` still exists on
  `gmarkop/elan_aphasia_classifier` (the project originally landed in the wrong
  repo). The owner agreed to delete it; the git proxy here rejects delete
  refspecs and this GitHub app has no `delete_branch` tool, so **only the owner
  can remove it**, via the GitHub branches page.
- The Autobahn incident provider has **never been run against the live API** —
  see "Unverified" below.

---

## Next session: agreed work (Thursday 3 September 2026)

Both were agreed after a question about riding with an iPad Pro and hosting the
app on a 24/7 Debian box at home. Do them in this order.

### 1. Persist the parsed route client-side (the one that matters)

Today the parsed route lives **only in server memory** (`RouteStore` in
`moto_route/api.py`). The service worker caches the app shell and map tiles, so
offline the UI loads — but the route is gone and the app comes back empty. That
is the difference between "offline-ish" and usable in an Alpine valley.

Plan: store the route payload (and the last successful live-layer responses) in
IndexedDB keyed by route id, restore on boot when the network is unavailable,
and show clearly that what is on screen is cached rather than live. The existing
`{available, reason, stale}` response shape already carries the vocabulary for
"this is old" — reuse it rather than inventing a second one.

### 2. `DEPLOY.md` plus a systemd unit

So it survives a reboot on the Debian box. Recommended path, already worked out:

- Bind uvicorn to `127.0.0.1` only; let Tailscale do the exposing.
- `tailscale serve --bg 8000` gives `https://<host>.<tailnet>.ts.net` with a real
  certificate. Requires MagicDNS + HTTPS enabled in the Tailscale admin console.
- Also document a Caddy + port-forward + DDNS variant for anyone not on Tailscale.

**Why HTTPS is non-negotiable:** service workers only register in a secure
context. Over plain `http://<ip>:8000` the offline tile cache silently does
nothing. Tailscale also solves the app's total lack of authentication by keeping
it off the public internet entirely — worth saying out loud in `DEPLOY.md`,
because an exposed instance is an open relay that will get the owner's IP banned
from the free Overpass/Open-Meteo/OSRM services.

Also note: the frontend uses absolute `/api` and `/static` paths, so it must be
served at the **root of a hostname**, not a subpath.

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

Then drive the real UI with Playwright. **Browser verification is not optional
here** — six real bugs were invisible to the unit tests and only showed up in a
live page. See below.

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
- **`xml.etree` expands entity declarations.** Route files never need a DTD, so
  `parsers/common.py` refuses one outright. Keep that guard on any new parser.

---

## Unverified

The **Autobahn incident provider** (`services/incidents.py`) was written against
the documented shape of Germany's Autobahn GmbH API and is covered by tests
using recorded fixtures, but the live endpoints were unreachable from the build
environment. It has never seen real data. It fails soft, so a wrong guess about
the schema shows an empty layer rather than breaking anything — but do not
present it as working until someone has watched it return a real incident.
