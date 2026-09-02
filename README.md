# Motorcycle Route Mapper

Load a GPX, KML or KMZ route, draw it on a map with its waypoints, and layer
live conditions on top: weather timed to where you will actually be, mapped
road closures, and alternate ways round.

It runs on your own machine. No account, no subscription, no API key.

![Sidebar with route stats, weather scores and closures beside a map of the route](docs/screenshot.png)

---

## Quick start

```bash
git clone https://github.com/gmarkop/motorcycle-route-mapper.git
cd motorcycle-route-mapper
pip install -r requirements.txt
python run.py
```

Your browser opens at <http://127.0.0.1:8000>. Drag `examples/dolomites_demo.gpx`
onto the drop zone to see everything working.

```bash
python run.py --offline      # map and route only, no network calls
python run.py --host 0.0.0.0 # reachable from your phone on the same wifi
python -m pytest             # run the test suite
```

Python 3.11 or newer.

---

## What it does

**Reads the files your planner actually produces.** Garmin BaseCamp hides the
calculated road geometry inside `<rtept><extensions><gpxx:rpt>` elements — miss
those and a Garmin route draws as straight lines between your stops. It also
distinguishes a *via point* (a place you chose to stop) from a *shaping point*
(a point that only exists to drag the line onto a nicer road), so the map is not
buried in markers you never cared about. Google Earth KML, KMZ archives, `gx:Track`
and multi-geometry placemarks are all handled.

**Times the weather to your ride.** The route is sampled every ~25 km, each
sample gets an arrival time from your departure and average speed, and the
forecast is read *at that hour*. Knowing it will rain in Cortina is useless;
knowing it will rain in Cortina at 14:00, when you get there, is the point.

**Scores conditions for two wheels, not four.** Each sample gets a *rideability*
score from 0 to 100 with the reasons spelled out. The weighting is deliberately
motorcycle-shaped: ice outranks everything, then thunderstorms and gusts, then
cold, then rain. A car's navigation app weights none of this the same way.

**Finds mapped closures.** Construction, gates, access restrictions and seasonal
roads within 150 m of your line, from OpenStreetMap via Overpass, each reported
at its position along the route ("closed gate at km 62").

**Compares alternates by corner count.** Alternate routes are measured for
*curviness* in degrees of heading change per kilometre, alongside distance and
time. "12 minutes longer, three times the corners" is usually the right trade on
a bike, and no car router will ever offer it to you.

Plus an elevation profile you can hover to see where you are on the map, and a
[Douglas-Peucker](https://en.wikipedia.org/wiki/Ramer%E2%80%93Douglas%E2%80%93Peucker_algorithm)
simplification pass so a 20 000-point track sends about 250 points to the browser.

---

## Honest limits

Worth knowing before you rely on any of it:

- **Closures are not live traffic.** OpenStreetMap knows about a pass gated shut
  for winter or a bridge under construction. It does not know about this
  morning's crash on the A8. There is no free pan-European live incident API;
  each national road authority runs its own feed, mostly without an open licence.
- **The ETA model is a constant average speed.** No traffic, no stops, no border
  queues. An hour-resolution forecast cannot tell the difference anyway, but
  a long day will drift — nudge the speed slider to see how much it matters.
- **The public OSRM demo server is rate-limited** and explicitly not for
  production use. If you lean on alternates, self-host OSRM and point
  `MOTO_OSRM_URL` at it.
- **Open-Meteo is free for non-commercial use.** Responses are cached for 15
  minutes on disk so normal planning stays well inside fair use.
- **Map tiles need a connection.** Leaflet itself is vendored locally, so the
  interface and your route work with no internet — only the tiles go blank.
- **No authentication.** `run.py` binds to localhost for that reason. Think
  before using `--host 0.0.0.0`.

---

## How it is put together

```
moto_route/
├── geo.py             Distances, bearings, simplification, sampling, curviness
├── models.py          GeoPoint / Waypoint / Route — the one shape everything speaks
├── config.py          Settings from environment variables
├── api.py             FastAPI endpoints and the in-memory route store
├── parsers/
│   ├── common.py      Namespace-agnostic XML helpers, and the XML-bomb guard
│   ├── gpx.py         GPX 1.0/1.1, tracks, routes, Garmin extensions
│   ├── kml.py         KML, KMZ, gx:Track, MultiGeometry
│   └── __init__.py    Format sniffing and dispatch
├── services/
│   ├── cache.py       TTL cache, memory then disk
│   ├── weather.py     Open-Meteo + the rideability score
│   ├── hazards.py     Overpass closures within a corridor of the route
│   └── alternates.py  OSRM alternates, ranked by corners
└── static/            Leaflet frontend — no build step, no framework
```

The shape of the thing: **parse once, enrich independently.** A file is parsed
into a `Route` and kept in memory under an id. The browser then asks for weather,
closures and alternates as three separate requests that load in parallel. A slow
Overpass query never delays the map, and any one service failing degrades to a
message in the sidebar rather than an empty screen.

### Ideas worth stealing from the code

If you are reading this to pick up Python patterns, these are the parts that
earn their keep:

**Match on the local name, not the namespace** (`parsers/common.py`). GPX 1.0,
GPX 1.1 and KML 2.2 all use different namespace URLs, and ElementTree reports
tags as `{namespace}localname`. Matching on the local name alone lets one parser
read every dialect instead of hard-coding URLs that a new exporter will break.

**Refuse a DTD before parsing** (`parsers/common.py`). `xml.etree` will happily
expand nested entity declarations until the process runs out of memory — the
"billion laughs" attack. A route file never needs a DTD, so rejecting one
outright costs nothing and closes the hole. Any code that parses XML from
outside your own program needs this.

**Bound your worst case, not just your average** (`geo.py`). Douglas-Peucker is
fast on a normal track and quadratic on a noisy one; a 20 000-point adversarial
file took minutes before a cheap radial pre-filter and an explicit evaluation
budget brought it to under two seconds. The budget degrades the *quality* of the
result rather than failing — usually the right shape for a limit.

**An explicit stack instead of recursion** (`geo.py`). The textbook
Douglas-Peucker is recursive and blows Python's recursion limit on a long track.
The iterative version is barely longer and simply cannot.

**Batch requests to somebody else's free service** (`services/weather.py`).
Open-Meteo accepts many coordinates in one call, which turns a 20-sample
forecast into one HTTP request instead of twenty. Cache keys round coordinates
to ~1 km and times to the hour, so two nearly-identical plans share an entry.

**Cache to disk, and keep the stale copy** (`services/cache.py`). When a live
request fails, an hour-old forecast beats no forecast — as long as the UI says
it is stale. Entries are written to a temp file and renamed, so a crash
mid-write cannot leave half a JSON document that later parses as valid.

**`0` is falsy and that will bite you** (`services/weather.py`). WMO weather
code 0 means "clear sky" — the best weather there is. Written as
`CODES.get(code or -1)`, it silently becomes "Unknown". There is a regression
test named after this exact mistake.

**Test judgement calls against fixed input** (`tests/test_weather.py`). The
rideability score is an opinion, so the tests assert *relationships* that must
hold — freezing rain must always score worse than a merely wet day — rather than
exact numbers that would break the moment you retune a weight. The HTTP layer is
an `httpx.MockTransport`, so the suite runs offline in under ten seconds.

---

## HTTP API

Useful if you want to script it or build your own frontend.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Version, offline flag, supported formats |
| `POST` | `/api/routes` | Upload a file (multipart `file`); returns `{id, route}` |
| `GET` | `/api/routes/{id}/weather` | `?departure=<ISO8601>&speed_kmh=<n>` |
| `GET` | `/api/routes/{id}/hazards` | Closures within the corridor |
| `GET` | `/api/routes/{id}/alternates` | Alternate routes, ranked |
| `GET` | `/api/routes/{id}/elevation` | Distance/elevation pairs for the profile |

Interactive documentation is generated at <http://127.0.0.1:8000/docs>.

Every enrichment endpoint answers with `{"available": bool, "reason": str, ...}`
rather than an error status when a service is down, so a failing layer is a
sentence in the sidebar instead of a broken page.

---

## Configuration

All optional, all environment variables.

| Variable | Default | What it does |
| --- | --- | --- |
| `MOTO_OFFLINE` | `false` | Skip every network call |
| `MOTO_WEATHER_URL` | Open-Meteo | Forecast endpoint |
| `MOTO_OVERPASS_URL` | overpass-api.de | Overpass endpoint |
| `MOTO_OSRM_URL` | router.project-osrm.org | Routing server — point at your own |
| `MOTO_CACHE_DIR` | `~/.cache/moto-route` | Disk cache location |
| `MOTO_SPEED_KMH` | `65` | Default average moving speed |
| `MOTO_WEATHER_INTERVAL_M` | `25000` | Distance between weather samples |
| `MOTO_MAX_WEATHER_SAMPLES` | `24` | Cap on forecast points per route |
| `MOTO_HAZARD_CORRIDOR_M` | `150` | How far off-route a closure still counts |
| `MOTO_SIMPLIFY_M` | `15` | Drawing simplification tolerance |
| `MOTO_TIMEOUT` | `20` | HTTP timeout, seconds |

Weather and hazard TTLs (`MOTO_WEATHER_TTL`, `MOTO_HAZARD_TTL`,
`MOTO_ROUTING_TTL`) and upload limits (`MOTO_MAX_UPLOAD`) are configurable too;
see `config.py`.

---

## Where to take it next

Roughly in order of value for effort:

1. **Fuel and coffee stops** — Overpass already knows every `amenity=fuel`.
   The corridor query in `services/hazards.py` is the pattern to copy.
2. **Cache map tiles** — a service worker storing tiles would make the whole
   app usable with no signal at all, which is the real touring case.
3. **Export the enriched route** — write a GPX back out with the weather
   warnings as waypoint descriptions, and load it onto the Garmin.
4. **National road-authority feeds** — several European countries publish open
   incident data. One provider module each, behind the same interface
   `services/hazards.py` already uses.
5. **A curviness heat map** — you already compute heading change per kilometre;
   colouring the route by it turns the number into a picture.

---

## Licence and attribution

The application code is yours to do as you like with. It stands on:

- [Leaflet](https://leafletjs.com) 1.9.4 (BSD-2-Clause) — vendored in
  `moto_route/static/vendor/leaflet/`, licence included
- [OpenStreetMap](https://www.openstreetmap.org/copyright) map data (ODbL)
- [OpenTopoMap](https://opentopomap.org) tiles (CC-BY-SA)
- [Open-Meteo](https://open-meteo.com) forecasts (CC-BY 4.0, free non-commercial)
- [OSRM](http://project-osrm.org) routing (BSD-2-Clause)

Respect their usage policies — they are the reason this has no subscription.
