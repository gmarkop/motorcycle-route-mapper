# Motorcycle Route Mapper — working notes

Self-hosted tour planner for European motorcycle trips. Loads a GPX/KML/KMZ
route, draws it, and layers live conditions on top. Runs on the owner's own
machine; no accounts, no API keys, no subscription.

Owner: gmarkop. Repo: `gmarkop/motorcycle-route-mapper` (**private**).

---

## Where things stand (8 September 2026)

`main` carries the app, the five touring features, offline route persistence,
and the whole self-hosted Overpass chapter. The owner runs it on a 2 GB Debian
box behind Tailscale.

### The app works end to end (9 September 2026)

Athens-Volos, 295 km, 18,078 recorded points, every layer through the running
app on the owner's 2 GB Debian box:

    curviness 5.3s   elevation 4.7s   alternates 3.6s   incidents 3.1s
    hazards   3.0s   pois      2.3s   weather    0.3s
    all layers together: 5.3s

Against **56.6 s** earlier the same day, and against a version that showed
weather and nothing else. Three faults, found in this order and each hidden by
the one before it: layers blocking the event loop (the route index), the
elevation layer spending a whole minute's API allowance (halved the samples),
and before both, a corridor that searched a fraction of the road.

The `--app` timing mode is what found them. Service checks answer "is Overpass
fast?"; only timing the layers together answers "why am I waiting", and those
two questions had silently come apart.

### The self-hosted Overpass is live and is the headline result

A tag-filtered Greece + Italy extract — 25 MB from ~2.3 GB of country data —
imported into `wiktorn/overpass-api` and reachable at `127.0.0.1:12345`.
Measured on the 389 km Pavliani route:

| | closures | points of interest |
| --- | --- | --- |
| own server | **10.6 s, 43 found** | 4.5 s, 187 found |
| overpass-api.de | 42.4 s, 27 found, a chunk lost | 49.4 s |
| overpass.kumi.systems | 20 of 32 chunks lost, 21 found | 81.4 s |

**Failing chunks on the public mirrors are the expected result, not a fault.**
Every endpoint is measured in turn so the fallback's behaviour is known; a
Greek or Italian route never reaches them. Do not go looking for a bug there.

### Next session: build the full extract (planned 8 September, to run 9th)

Two real trips are now driving the coverage, and between them they want nearly
every configured country:

* **June 2027, Greece to Poland** — via North Macedonia, Serbia, Hungary,
  Slovakia; back through Romania and Bulgaria. Hungary and Slovakia were added
  for this: the owner named the countries he is *going to*, and Serbia does not
  border Slovakia.
* **Germany** — Igoumenitsa-Venice ferry, then Austria, Germany, Belgium,
  Luxembourg, Switzerland. All already configured; France covers the
  Luxembourg-Switzerland leg, since those two do not border each other.

**The box is the constraint: 1.9 GiB RAM, 2.9 GiB swap, 134 GB disk.** That is
under the 4 GB the Overpass image documents as its minimum, and 17 countries is
roughly 18 GB of downloads filtering to ~200 MB — about eight times the only
import that has ever succeeded there.

The agreed plan:

1. Add a temporary 8 GB swapfile before importing, removed afterwards. Cheap,
   and swap is what decides whether the sort completes.
2. Download and filter all 17 in one run (disk and time only, no memory risk).
3. Import in two stages, because once every country is filtered the merge
   always includes all of them: `--only` the Poland trip's eight first, verify
   it serves, then the full build. Costs one extra import, and leaves a working
   eight-country server if the large one is killed.
4. Watch for the OOM killer: `sudo dmesg -T | grep -i "killed process"`. If it
   names `update_database`, fall back to the eight and treat Germany's
   countries as a separate build nearer that trip.

Rebuild both extracts in **May 2027**: `OVERPASS_META=no` means no incremental
updates, so data is frozen at build time and roadworks are exactly what moves.

### Immediately next, both unblocked

1. **POI categories** — motorcycle shops (`shop=motorcycle`,
   `shop=motorcycle_repair`), `amenity=motorcycle_parking` as a ranking signal,
   and accommodation. `motorcycle_friendly=yes` is a dead tag (the OSM wiki
   calls it "rarely tagged and not used by any real data consumer"), so the
   plan is to *add categories* rather than filter cafes. Counts were guesswork
   before; the local server makes them measurable in seconds. **Note the
   extract only holds the nine tags in `KEEP`** — new categories mean editing
   `build-extract.sh` and rebuilding.
2. **`claude/elevation-from-dem` is written but unmerged.** Independent of all
   the Overpass work. Fills heights from Copernicus GLO-90 when a GPX carries
   none — which is every file the owner's converter produces, so the elevation
   profile is currently blank for his own routes — and flags stretches that are
   twisty *and* steep.

Also unspent: the app itself has not been opened since the corridor fix landed.
Worth doing before anything else, since it is the thing actually used.

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

### Done: services verified from the Debian box (4 September 2026)

`tools/check_services.py` on the owner's server, using the 77 km demo route
(44 query points), everything reachable:

| endpoint | closure query | POI query |
| --- | --- | --- |
| overpass-api.de | 14.7 s, 35 elements | 12.4 s, 71 elements |
| overpass.kumi.systems | 55.4 s, 34 elements | 60.7 s, 71 elements |

Open-Meteo 0.2 s, OSRM 0.7 s, Autobahn 0.5 s.

So the reported `ConnectError` was a transient refusal under load, not a block,
and mirror rotation covers it. Two things this does **not** settle:

- **Cost on a real route.** 44 query points took 14.7 s; the cap is 350. A
  400 km tour lands in the low hundreds, and the kumi mirror is roughly four
  times slower — a failover on a long route could exceed the 90 s budget. Run
  `check_services.py --route <the real GPX>` to measure it.
- **Whether `around:` follows the line.** See below.

### Measured: long routes were timing out, and why (4 September 2026)

`check_services.py --route Pavliani_FR.gpx` (389 km, 169 query points) on the
owner's server:

| endpoint | closure query | POI query |
| --- | --- | --- |
| overpass-api.de | **504 after 10 s** | **504 after 10 s** |
| overpass.kumi.systems | **timed out at 105 s** | 90.5 s, 152 elements |

The cause was in the query, not the network. Walking the `around:` corridor is
what Overpass charges for, and the closure query walked the same 169-point
corridor **eight times**, once per tag filter; the POI query three times. Two
things were changed: long routes are chunked at `MOTO_OVERPASS_MAX_POINTS` (60)
with a one-point overlap so no seam is left unsearched, and the eight walks were
collapsed into two by collecting the corridor into named sets (`->.roads`,
`->.gates`) and filtering those.

**Chunking worked. Collapsing the walks did not — it was a pessimism, and has
been reverted.** See below.

**A misread worth remembering.** A second run from the box on 4 September showed
overpass-api.de passing at 39.5 s and 21.8 s where it had returned 504 the day
before — but that run was on `main` *without* the query change. Its own output
said "8 filters". That improvement was public-server load varying between two
days, and crediting the optimisation for it would have been wrong.

The same run also showed kumi.systems timing out at 105 s on both queries, so
the mirror is not a useful fallback for a long route.

### Measured: two walks are slower than eight (4 September 2026)

The next run from the box, on the collapsed query:

    closure query  WARN  311.0s across 3 chunk(s), 27 elements, 1 chunk failed
    POI query      PASS   66.9s, 187 elements

Chunks 2 and 3 answered and returned 27 elements between them; **chunk 1 failed
three times over**, and 311 s is almost entirely its retries at the full
105-second timeout.

Chunk 1 is the built-up end of the route, and that is the whole explanation.
`way(around:150,COORDS)["highway"]->.roads` materialises *every* road in the
corridor before any tag filter applies — thousands of ways through a town. The
eight-walk form never built that set: each filter sat inside its own `around`,
so Overpass answered it from the tag index and returned a handful of ways. Eight
cheap indexed lookups beat one broad fetch, and the query that *looks* wasteful
is the one that works.

So the shape is now a setting rather than an argument — `MOTO_OVERPASS_QUERY_STYLE`,
`filtered` (the eight-walk form, default, the one with evidence) or `grouped`
(kept so the two can be A/B measured on the real server). The POI query was never
affected: its tag filters were always inside the `around`.

### Measured: a deadline, and why it needs a fair share (4 September 2026)

311 s of retries is not a failure the rider should have to sit through, so
`MOTO_OVERPASS_DEADLINE` (120 s) now bounds what one layer may spend across all
its chunks and retries. Below `MIN_ATTEMPT_S` (20 s) remaining, no new query
starts — beginning one that cannot finish wastes the rider's time and the
server's alike — and each request's timeout is trimmed to what is left.

The first version divided nothing: it simply gave each chunk whatever remained.
Against a stub whose first chunk hangs forever it returned in 61 s instead of
311 s — and with `available: False` and zero hazards, because the one hanging
chunk had eaten the entire budget and chunks 2 and 3, which answer in about a
second each, never ran.

The fix is a fair share: each chunk gets `remaining / chunks_left`, floored at
`MIN_ATTEMPT_S`. Re-verified against the same stub:

    returned after 47s
      available: True | partial: True
      hazards from the chunks that DID answer: 1

One pathological section can no longer starve the rest, and the panel says how
much was skipped rather than sitting empty. `failed_chunks` now counts skipped
chunks as well as failed ones, and the note tells the rider to press Refresh to
try the rest.

**Still to confirm on the box.** The owner reported that the local app showed
weather only — no fuel, viewpoints, cafes or closures — for the Pavliani route.
The likely cause is exactly this multi-minute serialised wait, but that is a
hypothesis until they re-run after merging.

### Still open: does `around:` search the line or the points?

`check_services.py` has a probe for it and it has still never produced an
answer. The first run missed it entirely (a stale `/opt/moto-route` copy); the
second reached it but timed out, because the probe took the middle *half* of the
route, simplified it finely and sent it unchunked — a query more expensive than
any the app issues. It now takes a 25 km stretch capped at the chunk size, which
costs about as much as one ordinary chunk.

The checker also used to send the whole route as one query, measuring something
the app never does. It now chunks exactly as the app does and reports the total
across chunks.

The question matters: Douglas-Peucker leaves consecutive query points kilometres
apart on straight roads, so if `around:` searches near each coordinate rather
than along the line, most of a long route goes unsearched and closures are
missed with no error shown.

### Measured: why the slow layers felt slower than they are (7 September 2026)

The owner reported weather appearing instantly while fuel, cafes, viewpoints and
closures took "a considerable delay". Three separate causes, only one of which
was actually Overpass being slow.

**1. The two Overpass layers were queueing behind each other.**
`overpass_concurrency` was 1, and the semaphore is process-wide, so the closure
and POI requests — independent HTTP calls from the browser, fired in parallel —
serialised server-side. Measured against a stub holding every query 6 s, on a
route that chunks to one query per layer:

    MOTO_OVERPASS_CONCURRENCY=1  ->  12.0s
    MOTO_OVERPASS_CONCURRENCY=2  ->   6.0s

Exactly the 2x the serialisation predicts. The default is now 2, which is
Overpass's documented per-IP allowance rather than a gamble. It was 1 back when
a 429 was an unhandled crash; 429 is now retried with backoff and rotated onto
a mirror, so queueing everything costs more than it saves. `MOTO_OVERPASS_CONCURRENCY=1`
reverts it if the public servers start rate-limiting.

**2. A partial answer was cached for six hours — a bug, and mine.**
Both layers ran `cache.set(key, result, hazard_ttl_s)` unconditionally, so an
answer with missing sections was kept for six hours. The note added in the
previous round tells the rider "press Refresh to try the rest", and Refresh
would replay the same gaps out of the cache without ever reaching Overpass
again. The instruction was false the moment it was written. Partial results now
use `MOTO_PARTIAL_TTL` (300 s). Two tests cover it, and the first was checked
against the pre-fix code to confirm it actually fails there.

**3. Nothing on screen said the slow layers were working.**
The only feedback was the Refresh button's label; the fuel and closure panels
stayed `hidden` until their payload arrived, so on a long route the app looked
broken for a minute at exactly the moment it was working hardest. Those two
panels now show a pending line immediately. A panel that already has content —
a restored ride showing last night's answer — is dimmed instead of emptied,
because replacing usable data with "Searching…" hides information in order to
report progress.

The pending `<li>` carries a `pending` class, which matters beyond styling:
`tools/browser_test.py` waits on `#poi-list li` to decide a layer has loaded,
and without something to exclude, the placeholder satisfied that wait before
any data arrived. Every such selector in that file now says `:not(.pending)`.

Verified in headless Chromium against `tools/stub_apis.py` with the new
`STUB_OVERPASS_DELAY=6`: weather renders while both slow panels show their
pending line, and the line clears when the real rows land.

**Not addressed.** Chunks within one layer are still strictly sequential in
`run_chunked`, so a 3-chunk route is 3 round trips deep whatever the
concurrency. Running them concurrently would interact with the fair-share
deadline logic and is a bigger change than this round warranted.

### Measured: chunk-level concurrency, and the 3x that was never there

`run_chunked` ran a layer's chunks one after another, so a 5-chunk route was 5
round trips deep. They now run together under the same semaphore. Measured
end-to-end against a stub holding every query 3 s, 5 chunks per layer, both
Overpass layers requested at once:

| | sequential | concurrent |
| --- | --- | --- |
| `concurrency=2`, both layers cold | 15.1 s | **15.1 s** |
| `concurrency=2`, one layer cached | 15.0 s | 9.0 s |
| `concurrency=4`, both layers cold | 15.1 s | 9.1 s |
| `concurrency=4`, one layer cached | 15.0 s | 6.0 s |

**The first row is the important one, and it refutes what I predicted.** I told
the owner to expect "roughly another 3x". There is no 3x. At
`concurrency=2` with both layers cold — the ordinary case on public Overpass —
the semaphore is already saturated by the two layers, one slot each, and how
the chunks inside a layer are sequenced changes nothing. Wall clock is
`ceil(total_queries / concurrency) x query_time` either way.

What the change actually buys:

- **`overpass_concurrency` becomes a real dial.** Before, raising it above 2 did
  nothing at all (row 3: 15.1 s at both 2 and 4) because each layer could only
  ever occupy one slot. Now extra slots are used.
- **No idle slots.** When one layer is cached or fast, the other can use the
  whole semaphore instead of one slot (rows 2 and 4).
- The route to a genuinely faster app is therefore a self-hosted Overpass with
  a higher concurrency, not further work on this loop.

**A regression caught before shipping.** Deleting the fair-share allocation
looked justified — parallel chunks cannot starve each other. But where the
semaphore is narrower than the chunk count they still take turns, and a chunk
that hangs through its retries spends the time the queue behind it needed. So
each chunk keeps an `allowance`: the budget divided by `ceil(chunks /
concurrency)`, the number of turns the semaphore forces. At width 1 that is the
old fair share exactly; at width >= chunk count it is the whole budget, because
nothing is waiting. Verified at parity with the sequential code for budgets of
60, 70 and 90 s.

Two smaller things the concurrency forced:

- The deadline is now read **after** the semaphore is acquired, not before.
  Queueing for a slot is time off the budget, and a timeout computed before the
  queue would overrun it by however long the queue took. Harmless while queries
  were sequential; wrong the moment several chunks wait at once.
- The allowance bounds a chunk **across its retries**, from when it first wins a
  slot. Capping a single attempt lets three retries spend three times the share.

**A testing limit worth knowing.** `httpx.MockTransport` does not enforce HTTP
timeouts — the handler simply runs to completion. So "a chunk that hangs" cannot
be simulated with it, and any test that appears to prove timeout behaviour that
way is measuring handler duration instead. The deadline and allowance
arithmetic is tested by inspecting the timeout passed to the request
(`request.extensions["timeout"]["read"]`) and with a patched
`time.monotonic`, both of which exercise our own code rather than httpx's.

Tests keying on call *order* also had to be rewritten to key on query content:
with chunks running concurrently, "the first three calls" is no longer "the
first chunk's three attempts".

### Measured: the coverage box was wrong, and only a map showed it

The self-hosted Overpass work shipped with `MOTO_OVERPASS_COVERAGE`, a
south,west,north,east box saying what the local instance holds. The owner built
Greece + Italy and the script printed:

    MOTO_OVERPASS_COVERAGE=34.8172847,6.3886568,47.2518146,29.6000483

Those numbers are correct — 34.82 is Gavdos, 47.25 the Italian Alps, 6.39
western Piedmont, 29.60 Kastellorizo. The box is an accurate box. It is also
useless, because Greece and Italy are not a rectangle. Checking capitals
against it:

    Tirana, Zagreb, Ljubljana, Sarajevo, Podgorica, Belgrade, Sofia,
    Istanbul, Tunis  -> inside the box, absent from the data

Eight countries and a piece of North Africa would have been sent to the local
server, which answers HTTP 200 with an empty element list for ground it has
never seen — indistinguishable from a road with no closures. And they are not
arbitrary countries: Slovenia, Croatia, Bosnia, Montenegro and Albania are
exactly what you cross riding overland from Italy to Greece.

Coverage is now the Geofabrik `.poly` clipping polygons, downloaded beside each
extract by `build-extract.sh` and tested with ray casting in
`moto_route/coverage.py`. They are the exact shape of the import — slightly
generous, since Geofabrik buffers them past the border, which errs in the safe
direction.

**Every point is tested, not the bounding box.** A Bari-to-Igoumenitsa ride has
both ends in the data and its middle in Albania, and its bounding box lies
entirely inside the covered rectangles. `endpoints_for` therefore takes the
coordinates the layer is about to search rather than a box, which also removed
the corridor-margin fudge the box version needed.

The box setting is kept for a mirror whose coverage really is a rectangle, and
documented as wrong for a set of countries.

**The lesson worth keeping.** Both versions had tests, and the box version's
tests passed — because they asserted the behaviour of a box, using points
chosen to be clearly inside or clearly outside it. Nothing in the suite knew
that the space between Greece and Italy contains Albania. The bug was
geographic, and only checking the abstraction against the actual world found
it. When a setting encodes a fact about the physical world, test it against the
world, not against itself.

Also of note: `Settings` is a `@dataclass(slots=True)`, so `cached_property`
raises `TypeError: No '__dict__' attribute`. Memoisation there has to be a
module-level `lru_cache` keyed by the setting's value.

### Answered at last: the corridor had a hole, and it was arithmetic

The `around:` question has been open since the beginning. The owner's Debian
box finally ran the probe on a 389 km Greek route:

    highways found with dense coordinates: 86
    highways found with only the two ends: 36
    FAIL  `around:` searches the points, not the line — only 42% found

**That verdict was not safe to trust, and the probe has been fixed.** It took a
stretch from the middle of the route, which on a mountain road is curvy — and
on a curvy road *both* candidate readings collapse. "Circles around each
coordinate" and "a corridor along the straight chords between them" lose the
road equally when the coordinates are thinned, so the experiment could not tell
them apart. It now searches for the *straightest* 8 km stretch on the route,
where the two readings predict the same corridor and a collapse can only mean
circles, and warns when no straight enough stretch exists.

**But the real bug needed no probe at all.** The constants decided it:

    corridor radius     150 m   (hazard_corridor_m)
    simplify tolerance  250 m   (_QUERY_SIMPLIFY_M)

Douglas-Peucker guarantees only that the road lies within the tolerance of the
thinned line. A 250 m tolerance inside a 150 m corridor therefore leaves up to
100 m of real road outside the searched corridor on every curve it cut — under
the *favourable* reading. Under the other, `_MAX_QUERY_POINTS = 350` spread
over 389 km puts coordinates 1.1 km apart, nearly four times too far for 150 m
circles to touch.

Either way the closure layer had not been searching most of a long route, and
said nothing: the panel showed what it found, with no error and no note.

`geo.corridor_points(points, radius_m)` replaces both fixed constants. It
simplifies to half the radius (bounding how far the road can stray from the
line) and then interpolates so no gap exceeds 1.5x the radius (so circles
overlap). Satisfying both costs no more than the stricter one, so the open
question stops mattering.

Order matters, and the first attempt had it backwards: densify-then-simplify
keeps every point of a 1 Hz track log on a straight road — 100 recorded points
where 50 are needed. Simplify first, then fill the gaps thinning left.

`sample_every` cannot be used for the filling: it returns only coordinates the
route already has, so a GPX with points 2 km apart still yields 2 km gaps.
`_interpolate_along` adds points on the segments instead.

**The cost is real and lands almost entirely on closures.** Spacing follows the
corridor, so the 1 km fuel corridor barely changes while the 150 m closure
corridor goes from 3 chunks to roughly 30 on the Pavliani route. That is the
honest price of searching the road instead of a tenth of it, and it is a strong
argument for the self-hosted Overpass: 30 chunks locally is seconds, on the
public servers it is not affordable. Long routes against public Overpass will
now report partial results — which is not new breakage but the pre-existing gap
finally becoming visible.

### Confirmed on real data: the corridor gap was 19% of the closures

The Pavliani route (389 km) against the owner's self-hosted Overpass, before
and after `geo.corridor_points`:

| | query points | chunks | time | closures found |
| --- | --- | --- | --- | --- |
| fixed 250 m thinning | 169 | 3 | 10.7 s | 36 |
| corridor-derived | 1883 | 32 | **10.6 s** | **43** |

Seven closures on one route — nearly a fifth — were being missed, with no error
and nothing in the panel to suggest anything was absent. That is the whole
argument for the change, and it took a real route on real data to produce it:
the fault was provable from the constants alone, but not its size.

**The extra chunks cost nothing.** Ten times the coordinates in the same wall
clock, because a local Overpass answers each chunk in a fraction of a second
and eight run at once. The same route through the public servers lost 20 of 32
chunks and returned 21 closures — half of what is there. Full corridor coverage
on a long route is a self-hosted feature; on the public servers it is honestly
partial, which is at least visible.

### The checker drifted from the app twice, the same way

Both times a change in the app left `tools/check_services.py` describing
something the app no longer does, and both times nothing caught it:

* `_query_coordinates` gained a `settings` argument. The checker's call was not
  updated, `pytest` does not import `tools/`, and it surfaced as a `TypeError`
  on the owner's server three merges later. Fixed with `--dry-run` and
  `tests/test_tools.py`.
* The checker built *both* layers' queries from the closure layer's
  coordinates. Harmless while the two thinned identically; after the corridor
  fix the POI query was reported at 32 chunks when the app sends about 5.

The second is the more instructive: nothing was broken, the numbers were just
about a different program. A diagnostic claiming "as the app sends them" has to
be re-read whenever the app changes how it sends them.

The checker also forced `overpass_concurrency=1`, which measured a cadence no
rider experiences. It now uses `settings.concurrency_for()` per endpoint, the
same 8-local/2-public split the app applies.

### The tools are code, and nothing was testing them

Changing `_query_coordinates` to take `settings` updated both call sites in the
app and missed the one in `tools/check_services.py`. 290 tests stayed green,
because the suite never imports `tools/`. It surfaced as a `TypeError` on the
owner's server, mid-setup, three merges after the change.

Two things now close that gap:

`check_services.py --dry-run` builds every query and sends none — route
parsing, the app's coordinate thinning, chunking, query building — which is
where the wiring between tool and app lives. It is also the honest answer to
"what is this about to ask for?" before pointing the checker at a public server
with a 600 km route.

`tests/test_tools.py` runs each tool as a subprocess: syntax, `--help`, the dry
run, and the corridor invariant read back out of the tool's own output. Checked
against the broken call to confirm it fails there.

The wider point, having now cost this several times: work that cannot be
executed in this environment gets no feedback, and the diagnostics were exactly
that — written to be run on a machine with a network, never run here. Anything
in `tools/` or `deploy/` needs a way to be exercised locally, however partial,
or it is being written blind.

### The owner's actual usage decides the timeouts (8 September 2026)

A German route on the phone showed weather and then gave up: Overpass out of
time, elevation rate-limited. Germany is outside the Greece + Italy coverage,
so both Overpass layers fell back to the public servers, where the
corridor-dense coordinate list is expensive — and the 120-second budget, tuned
for a rider standing at a petrol station, cut it off.

**He does not use it that way.** Routes are consulted the evening before the
ride, from an armchair. Latency is nearly free; a closure that never appeared
is not. That single fact settles a question no amount of measurement could:
when the two conflict, wait rather than truncate.

So the budget follows the server, like the concurrency already did.
`MOTO_OVERPASS_DEADLINE` (120 s) is for your own instance, which is fast enough
that it never binds. `MOTO_OVERPASS_PUBLIC_DEADLINE` (600 s) is for a fallback
route. Note this raises the effective default for anyone with no local server
from 120 s to 600 s, deliberately.

**Do not "fix" this by thinning the corridor for public routes.** That trades a
visible wait for an invisible gap, which is the bug this project has already
spent a week removing.

### Two layers asked for the same heights at once

The 429 was not Open-Meteo being stingy. `/elevation` and `/curviness` are
separate endpoints the page requests together, and both call
`elevation.profile` for the same route. Neither has populated the cache when
the other starts, so both fetched every batch and the second was rate-limited.

`elevation.lookup` now shares the in-flight request: a second caller awaits the
first rather than repeating it. Batches are also capped at two at a time and a
429 is retried with backoff, honouring `Retry-After`, instead of being
surfaced. Verified by removing the sharing and watching the test fail.

A cache alone cannot fix this. Nothing is in it until the first answer returns,
and the whole problem happens before then.

### Every layer took 51 seconds because one of them held the loop

The `--app` timing mode, added because the service checks and the rider's
experience had stopped agreeing, gave the answer in one run on a 295 km Greek
route:

    curviness  56.6s   elevation 56.1s   weather 52.7s   alternates 51.7s
    incidents  51.2s   hazards   51.0s   pois    50.5s

Everything lands together at ~51 s — including `incidents`, which for a Greek
route returns "no feed covers this route" without touching the network, and
`pois`, whose Overpass query measured 1.1 s. Layers doing no work waited exactly
as long as layers doing all of it. That shape is not seven slow layers; it is
one thing holding the event loop while the rest queue.

Profiled on an 18,078-point route: `_elements_to_pois` 5.3 s and
`_elements_to_hazards` 8.9 s, both pure CPU inside async handlers. 235 fuel
stops against 18,000 segments is four million distance tests, and the hazard
path was worse — a full polyline scan for the distance, then a *second* full
scan for the nearest vertex, per vertex, per hazard. The owner's box is slower
than the machine this was measured on, which is how 14 s becomes 51.

`geo.RouteIndex` buckets segments into a grid sized to the caller's reach, so a
lookup tests the containing cell and its eight neighbours instead of the whole
route. Measured on the same data: **14.21 s of blocking CPU became 0.16 s**, with
identical answers on all 235 probes.

**Read the shape of a timing table before reading the numbers.** Everything
finishing together means contention, not slowness, and no amount of optimising
the slowest row would have found this — the slowest row was a symptom.

Still worth doing: the remaining synchronous work should move off the event
loop with `asyncio.to_thread`, so that a genuinely expensive layer delays only
itself. The index removed the pain; it did not remove the coupling.

### The elevation 429 was arithmetic, not bad luck

After the route index took the app from 56.6 s to 8.2 s, elevation was the last
failing layer — still rate-limited, still 429, on every load of a new route.

Open-Meteo's free tier allows 600 calls a minute, and a request carrying many
coordinates is counted as though those coordinates had been fetched in a loop.
`max_elevation_samples` was **600**. One layer, on one route, spent the entire
minute's allowance — so it was refused every time, never cached a result, and
therefore failed again on the next load. It could not have worked.

Now 300, which leaves room for the weather layer and for looking at a second
route. `MAX_CONCURRENT` also drops from 2 to 1: with a coordinate-weighted
limit, parallel batches do not reduce what a route costs, only how fast it is
spent, and a burst is the shape most likely to be refused.

The cost is resolution — a height every ~1 km on a 295 km route rather than
every ~500 m. A short sharp ramp is smoothed away; a mountain pass, which is
what the demanding-stretches panel exists for, is not. A self-hosted terrain
model would lift the limit entirely if that ever matters.

**Confirmed on the box.** Open-Meteo documents that the weighting exists but
not its exact form, so "600 samples = 600 weighted calls" was read from their
guidance plus the symptom rather than from a specification — and the test was
stated in advance: if 429s persisted at 300 the weighting was not the
mechanism. They did not. Elevation passed at 4.7 s on the route that had failed
every previous load.

### Open ideas, nothing agreed

More incident providers; a `MOTO_TILE_URL` setting (the tile server is hard-coded
in `mapview.js`, and the docs had to be corrected to say so); multi-day tours;
rider-tuned rideability weights.

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
- **The public Overpass servers refuse connections per query, not per client.**
  The heavy closure query (8 filters, `out geom`) gets turned away while the
  lighter POI query to the same host succeeds — which looks impossible until you
  know it. Attempts rotate through `MOTO_OVERPASS_FALLBACK_URLS`.
- **An unset environment variable and one set to empty are different.**
  `_env_list` used to collapse them, which made a non-empty default impossible
  to switch off.
- **The diagnostic tools take no privileges, and must not be run with sudo.**
  `check_services.py` reads a route file from the user's home directory, and the
  `motoroute` service account is deliberately shut out of `/home` — so
  `sudo -u motoroute` fails on exactly the file the user wants to test. DEPLOY.md
  once said to use sudo and caused this.
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

## POI categories, decided by census (2026-09)

`tools/tag_census.py` counted every candidate tag along two real routes,
against a public server rather than the local extract -- the extract holds only
the tags `KEEP` was told to keep, so a candidate would have come back zero
meaning "not imported", not "not there".

Per 100 km, Athens-Volos (295 km, motorway) and Bolzano-Cortina (77 km, Alps):

| tag | Greece | Italy | verdict |
| --- | --- | --- | --- |
| `amenity=fuel` | 37.3 | 11.7 | shipped already |
| `amenity=cafe` | 40.4 | 86.1 | shipped already |
| `tourism=viewpoint` | 1.0 | 54.8 | shipped already |
| `tourism=hotel` | 8.1 | 124.0 | **built** |
| `tourism=guest_house` | 0.7 | 14.4 | **built** |
| `amenity=motorcycle_parking` | 0.0 | 11.7 | **built** |
| `tourism=camp_site` | 0.7 | 0.0 | built, rides free in the same selector |
| `tourism=motel` | 0.0 | 0.0 | built, common further north |
| `shop=motorcycle` | 0.3 | 0.0 | rejected |
| `shop=motorcycle_repair` | 0.0 | 0.0 | rejected |
| `service:motorcycle:repair` | -- | 0.0 | rejected |
| `shop=motorcycle_parts` | -- | 0.0 | rejected |
| `motorcycle:theme` | 0.0 | 0.0 | rejected |
| `motorcycle_friendly` | 0.0 | 0.0 | rejected |

Findings worth keeping:

- **Motorcycle-friendly cafes and hotels cannot be built.** `motorcycle_friendly`
  and `motorcycle:theme` are zero on both routes, matching the OSM wiki's own
  "rarely tagged and not used by any real data consumer". A good idea with no
  data behind it.
- **One country is not a measurement.** `amenity=motorcycle_parking` is zero in
  Greece and 11.7 / 100 km in Italy. It was written off on the Greek run and
  the Italian run brought it back.
- **`service:motorcycle:repair` did not rescue repair.** The hypothesis was that
  `shop=motorcycle_repair`'s zero was a tagging scheme rather than an absence.
  It is an absence.
- **Viewpoints are 55x denser in the Alps than along the A1** (1.0 vs 54.8).
  Partly route type, partly how thoroughly South Tyrol is mapped. A motorway
  route is a poor place to judge any scenic category.
- **A dense category cannot share a cap with a sparse one.** Accommodation runs
  124 / 100 km in the Dolomites against 11.7 for fuel, so one shared 300-place
  budget would be spent on hotels early in a long route and drop the later fuel
  stops. `plan_fuel_stops` reads that list, so the symptom would not have been a
  short list -- it would have been an invented fuel gap. Caps are per category
  (`Settings.poi_limit`), pinned by a test that fails with 1 of 10 fuel stops
  surviving under the old shared cap.
- **`KEEP` and `pois.CATEGORY_TAGS` must agree**, and now a test says so. A tag
  queried but not kept returns nothing from the local server, which reads as
  "none along this route".

**The local extract must be rebuilt before the new categories work locally.**
Greece and Italy hold no accommodation or parking tags until then; routes there
will show empty Sleep and Parking chips while a public fallback would fill them.

### After every extract rebuild

`python tools/check_extract.py` counts every tag the app queries against your
own server, across the whole coverage area. Run it before trusting a route.

The two tools are opposites and both are needed:

| | asks | answers |
| --- | --- | --- |
| `tag_census.py` | a public server | does this tag exist in the world? |
| `check_extract.py` | your own server | did my build actually keep it? |

`tests/test_docs.py` already fails if `KEEP` and `pois.CATEGORY_TAGS` disagree.
What no test can check is whether the build you ran and the import you did put
that data on the server that is running -- three separate steps, each of which
has silently not happened at least once in this project.

### The rebuild that did nothing (2026-09)

KEEP gained the accommodation and parking tags, the rebuild ran, and the server
came back holding **17 hotels across Greece and Italy**. The census had found 95
along one 77 km Dolomites road.

`build-extract.sh` cached filtered countries by filename alone:

```bash
[ -f "$out" ] && { echo "    $name: already filtered"; continue; }
```

The filtered files predated the KEEP change, so every country reported "already
filtered", `osmium tags-filter` never ran, and the merge rebuilt the extract
from the old tag set. Everything else was green: KEEP had the tags, and
`test_docs.py` confirmed KEEP and the app agreed.

Two things now stop it:

- The signature of KEEP is stored beside each filtered file. A change re-filters,
  and a filtered file with no signature is re-filtered rather than trusted. The
  merge refuses stale inputs instead of quietly including them.
- `check_extract.py` no longer treats any non-zero count as a pass. **Zero is not
  the only way an import fails**: a tag left out of the filter still arrives in
  small numbers, as members of relations that were kept, so 17 hotels looked like
  success. Tags sharing a key sit within an order of magnitude of each other in
  real data, so anything under 1/100th of its healthiest peer is reported. The
  closest healthy real case measured is `highway=construction` at 1/58 of
  `barrier`, which is why the cutoff is 1/100 and not 1/10.

`tests/test_build_extract.py` runs the real script against a local stand-in for
Geofabrik with real osmium, and fails on the old script in both directions:
a changed KEEP must re-filter, an unchanged one must still reuse.

### The download that resumed into a different file (2026-09)

The rebuild after the KEEP fix stopped with:

    PBF error: invalid BlobHeader size (> max_blob_header_size)

`build-extract.sh` ran `curl -fL -C -` unconditionally on every build, against
a raw file that was already complete. Geofabrik regenerates each extract daily
and it grows, so the resume asked for "bytes N onward", received bytes N onward
of the **newer, larger** file, and appended them to the older file's first N
bytes. The result is the right size and unreadable.

Reproduced end to end against a Range-serving stand-in: the old script produces
a 302-byte file where today's real file is also 302 bytes, matching neither
day, and osmium fails with exactly that message. It had been sitting in the
raw directory for several builds, invisible, because the filtering step was
being skipped -- so nothing ever opened the file. One silent bug hid another.

Downloads now resume into a `.part`, are checked against Geofabrik's published
`.md5`, are opened with `osmium fileinfo` before being trusted, and only then
moved into place. A file in `raw/` is therefore one that has been verified,
which is what makes skipping it on the next run honest. On a mismatch it starts
over once from nothing rather than resuming the damage.

`osmium fileinfo -F pbf` is required, not tidiness: osmium picks its reader
from the file extension, and the extension is `.part`. Without it every good
download is rejected as unreadable and fetched forever -- caught by running it,
not by reading it.

### The rebuild that worked (2026-09), and the checker crying wolf

Greece + Italy, after re-filtering and re-importing:

| tag | before | after |
| --- | --- | --- |
| `tourism=hotel` | 17 | 32,727 |
| `tourism=guest_house` | 23 | 15,861 |
| `tourism=camp_site` | 2 | 2,966 |
| `amenity=motorcycle_parking` | 2 | 5,471 |
| `tourism=motel` | 0 | 266 |

`tourism=motel` at 266 tripped the 1/100 ratio rule against 32,727 hotels --
and is simply true. Motels are a North American idea; the census had already
measured zero along both routes. A checker that cries wolf is worse than none,
because a real failure hides among the false alarms.

The ratio now reports rather than fails, and the reason is worth keeping: every
tag of the import that actually broke came back **under a hundred across two
whole countries** -- 17 hotels, 23 guest houses, 2 camp sites, 2 parking. The
absolute floor caught all five. The ratio caught none of them, and produced the
only false alarm. So the floor is the verdict and the ratio is a question.

Settling that question needs a public server, and the first version asked it
for every hotel between Tunisia and Ukraine, which hung. It is now opt-in
(`--second-opinion`) and asks about a 1.5-degree sample box, found by asking
the local server which areas actually hold data -- free, and guaranteed to be
inside the coverage rather than out in the Adriatic. Shares are what get
compared, not counts.

- motel: 0.0081 per hotel here, 0.0085 there — rare, not missing.
- hotel in the broken extract: 0.00095 per viewpoint here, 3.41 there — a
  factor of 3,600, and unambiguous.

The tolerance is a factor of ten, deliberately loose: regions really do differ,
and the gap worth catching is thousands, not tens.

### The census and the app agree (2026-09-11)

Athens-Volos through the running app, against what `tag_census.py` had measured
along the same route days earlier on a public server:

| category | census | the app |
| --- | --- | --- |
| fuel | 110 | 110 |
| accommodation | 28 | 29 |
| viewpoint | 3 | 3 |
| cafe | 119 | 96 |
| motorcycle_parking | 0 | 0 |

Fuel matching exactly -- same route, same 1 km corridor, different servers --
is the strongest evidence available here that the local extract holds what the
public one does along a real route. Accommodation is the same story at the same
corridor.

Cafe is lower by design, not by error: the app searches 300 m for a cafe and
1000 m for fuel, while the census asked at 1000 m throughout. Worth remembering
before reading a future gap as a fault.

`motorcycle_parking: 0` is correct. Greece has none along this route and Italy
has 11.7 per 100 km, which is why two countries were measured before building
it.

**`git pull` does not update the running service, and this cost a round here
again.** The app kept answering with three categories while the extract held
five, and `check_extract.py` could not have noticed: it verifies the server,
not what the app asks the server for. The cache needed no clearing, though --
the POI cache key is the query text, so a changed query misses by construction.

### 98 of 100 (2026-09-11)

A Greek route returned 98 places to stay against a cap of 100. Two more and the
old behaviour would have kept the first 100 in route order and dropped the
rest -- which does not read as a truncated list, it reads as a stretch of road
with nowhere to sleep. On the far half of a ride, that is the worst place for
the app to be quietly wrong.

Categories are now thinned across the route (every Nth) rather than truncated
at the front, and the payload carries `thinned` so the chip can say
"100 of 340" instead of a bare 100. Verified end to end: 340 hotels over a
75 km route keep coverage from km 0 to km 75, where truncation would have
stopped at km 22.

Same shape as the fuel-plan bug, and the same lesson: a partial answer that
does not say it is partial is indistinguishable from a complete one.

**Accommodation is on by default** (2026-09-11). It shipped off, reasoning that
Alpine hotel density would bury the fuel stops. The owner then went looking for
it twice and found an empty map both times. These routes are planned the
evening before a multi-day ride, where a bed is what you came for. Density is
handled by the thinning above, not by hiding the category.

### Closures on the route, not near it (2026-09-11)

The panel was cluttered with closures that are not on the ride, and the obvious
fix -- a narrower corridor -- would not have worked. `_project_onto_route`
takes the hazard's *closest* vertex, so a closed side road meeting the route at
a junction reports **zero metres off route**. No corridor width excludes
something that is touching.

The question that separates them is not how close the nearest point is but how
much of the way keeps company with the route: a junction contributes one
segment, the road you are riding contributes its length. `_runs_along_route`
measures that, requiring 200 m alongside or 60% of a short way's length, so a
closed 80 m bridge on the route is not dismissed for being short.

Searched wide, shown narrow -- the same shape `pois.py` uses. The 150 m search
corridor is unchanged, because shrinking it is a recorded **don't**: a 250 m
thinning inside a 150 m corridor is what once left most of a 389 km route
unsearched. `MOTO_HAZARD_ON_ROUTE_M` (60 m) decides what is shown.

Nothing is dropped silently. The payload carries `nearby`, the note says "3
more closures are within 150 m of the route but on other roads", and an empty
panel distinguishes "nothing closed on this route, 3 nearby" from "no closures
at all" -- which are different answers, and the second is the reassuring one.

### Why a deploy took two reloads to appear (2026-09-11)

A layout change went live and the browser kept showing the old one. Not the
install, not the server -- the service worker.

```js
const VERSION = 'v1';                       // never changed, all project long
const SHELL_CACHE = `moto-shell-${VERSION}`;
```

The shell is served stale-while-revalidate, so every deploy reused the same
cache: the first load rendered the old CSS and fetched the new one for next
time. Two reloads to see any front-end change, and nothing anywhere said so --
which reads as a deploy that did not happen, and has been mistaken for one.

`/sw.js` is now served by the app with `VERSION` replaced by a sha256 of the
shell assets themselves. The cache name changes exactly when what it holds
changes, and not on a restart that altered nothing.

**The tile cache is deliberately not versioned.** `activate` deletes every
`moto-` cache that is not current, so a versioned tile cache name would throw
away the map a rider downloaded for a route with no signal, on every update.
Its name is kept exactly as first shipped so existing caches survive this
change too. A test fails if anyone versions it.

