/* Entry point: wiring, orchestration, and the two pieces of UI that do not
 * belong to any single layer (the elevation profile and the offline cache).
 *
 * The load sequence is deliberate. The file is parsed and drawn first, because
 * that is what the user came for and it needs no network. Only then do the live
 * layers go out, all at once and independently, so a slow Overpass query never
 * holds up the map and a dead service is one sentence in one panel.
 */

import * as mapview from './mapview.js';
import * as panels from './panels.js';
import * as tiles from './tiles.js';
import * as store from './store.js';
import { curvinessGradient } from './format.js';

const $ = (id) => document.getElementById(id);

const state = {
  routeId: null,          // server-side id; ephemeral, dies with the server
  rideKey: null,          // client-side id; stable across re-uploads
  profile: null,
  inFlight: null,                                   // AbortController
  // All five on. Accommodation started off, on the theory that 124 hotels per
  // 100 km of Dolomites would bury the fuel stops -- but the owner went
  // looking for it twice and found an empty map both times, which is the
  // stronger evidence. These are multi-day routes consulted the evening
  // before a ride, and a bed is a thing you came to find rather than a thing
  // you would rather not see. Density is handled where it belongs now: a
  // crowded category is thinned across the route and the chip says so.
  poiFilter: new Set(['fuel', 'cafe', 'viewpoint', 'motorcycle_parking',
                      'accommodation']),
  poiPayload: null,
  curvinessOn: false,
  // Twisty *and* steep stretches. They arrive on the curviness
  // payload while the profile arrives on the elevation one, from
  // different endpoints in either order, so each redraws when the
  // other lands rather than assuming it got there first.
  demanding: [],
  recoveryError: null,
  servedFromCache: false,
  config: {
    speed_kmh: 65, tank_range_km: 250,
    max_cached_tiles: 250, tile_prefetch_delay_ms: 120,
  },
};

/** The live layers, and the panel each one falls back into when it fails. */
// `waiting` is the line shown while the layer is still being fetched. Only the
// two Overpass-backed layers get one: everything else answers in well under a
// second, and a message that flashes past is noise rather than feedback.
// Without it these two panels stay hidden for a minute or more on a long route
// and the app looks broken at exactly the moment it is working hardest.
const LAYERS = {
  weather: { panel: 'weather-panel', list: 'weather-list' },
  pois: {
    panel: 'fuel-panel', list: 'poi-list',
    waiting: 'Searching OpenStreetMap for fuel, coffee, views, parking and places to stay…',
  },
  hazards: {
    panel: 'hazard-panel', list: 'hazard-list',
    waiting: 'Searching OpenStreetMap for closures and roadworks along the route…',
  },
  incidents: { panel: 'incident-panel', list: 'incident-list' },
  alternates: { panel: 'alternate-panel', list: 'alternate-list' },
};

/**
 * Show that a slow layer is being worked on, before its first byte arrives.
 *
 * Long routes are split into several Overpass queries, so this can run for a
 * minute. Saying so beats an empty panel, and saying it *in* the panel beats
 * only greying out the Refresh button somewhere else on the page.
 */
function markPending(name) {
  const { panel, list, waiting } = LAYERS[name];
  if (!waiting) return;
  const target = $(list);

  // A restored ride already has last night's answer on screen. Replacing that
  // with "Searching…" would hide usable data in order to report progress, so
  // a populated panel is only dimmed; an empty one gets the message.
  if (!$(panel).hidden && target.children.length) {
    target.classList.add('refreshing');
    return;
  }
  $(panel).hidden = false;
  // The `pending` class matters beyond styling: tools/browser_test.py waits on
  // "a list item exists" to know a layer has loaded, and without something to
  // exclude, this placeholder would satisfy that wait before any data arrived.
  target.innerHTML = `<li class="muted tiny pending">${waiting}</li>`;
}

/** Undim a panel once its layer has settled, however it settled. */
function clearPending(name) {
  const entry = LAYERS[name];
  if (entry && entry.waiting) $(entry.list).classList.remove('refreshing');
}

// -------------------------------------------------------------- file loading

function wireUpload() {
  const zone = $('drop-zone');
  const input = $('file-input');

  $('browse').addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    if (input.files.length) upload(input.files[0]);
  });

  // Without preventDefault on dragover the browser navigates away to the file.
  ['dragenter', 'dragover'].forEach((event) => zone.addEventListener(event, (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach((event) => zone.addEventListener(event, (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
  }));
  zone.addEventListener('drop', (e) => {
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
  });
}

async function upload(file) {
  panels.showError('');

  try {
    const payload = await postRoute(file);

    state.routeId = payload.id;
    state.servedFromCache = false;
    showRoute(payload.route);

    // Keep the file itself, not just the parsed result: the server's route ids
    // do not survive a restart, and holding the bytes lets the app re-upload
    // silently instead of asking for the GPX again.
    state.rideKey = await persist(file, payload);
    await refreshSavedList();

    loadElevation();
    loadCurviness();
    refreshLiveData();
  } catch (err) {
    panels.showError(err.message);
  }
}

async function postRoute(file, filename) {
  const body = new FormData();
  // The name is passed explicitly. A File carries its own, but a plain Blob
  // does not, and the server needs the extension to know how to parse it —
  // FormData would otherwise call it "blob" and the upload would be rejected
  // for a reason that has nothing to do with the file's contents.
  body.append('file', file, filename || file.name || 'route.gpx');
  const response = await fetch('/api/routes', { method: 'POST', body });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(describeUploadFailure(response.status, payload));
  }
  return payload;
}

/** A rejected upload, in words a rider can act on. */
function describeUploadFailure(status, payload) {
  const detail = payload && payload.detail;
  if (typeof detail === 'string') return detail;
  if (status === 422) {
    // FastAPI's validation error, i.e. the request never reached our own
    // handler. From here that means the file part was not sent as a file —
    // a stored ride whose contents did not survive, most likely.
    return 'The server could not read the uploaded file (422). '
      + 'If this happened while restoring a saved ride, the saved copy is '
      + 'unusable — load the GPX again.';
  }
  if (status === 413) return 'That file is too large for the server.';
  return `Upload failed (HTTP ${status}).`;
}

/** Draw a route and reveal the panels that only make sense once one is loaded. */
function showRoute(route) {
  mapview.drawRoute(route);
  panels.showSummary(route);
  $('plan').hidden = false;
  $('offline-panel').hidden = false;
}

async function persist(file, payload) {
  if (!store.supported()) return null;
  try {
    return await store.saveRide({
      serverRouteId: payload.id,
      file,
      route: payload.route,
      plan: currentPlan(),
    });
  } catch (err) {
    // Persistence is a convenience, never a precondition. A full or disabled
    // store must not stop the ride being planned.
    console.warn('Could not save ride for offline use:', err);
    return null;
  }
}

// -------------------------------------------------- recovering a lost route id

let recovery = null;

/**
 * Re-upload the stored file after the server has forgotten this route.
 *
 * The route store in api.py is in-memory and bounded, so a server restart — or
 * simply loading twenty other routes — makes every id 404. Five layer requests
 * run in parallel and would each trip the same 404, so the recovery is shared:
 * whoever asks first does the upload, the rest await the same promise.
 */
function recoverRouteId() {
  if (!recovery) {
    recovery = (async () => {
      const ride = state.rideKey ? await store.getRide(state.rideKey) : null;
      if (!ride || !ride.file) return false;

      // Checked before sending rather than after being rejected. A record
      // written by an older version of this app, or one whose contents did not
      // survive being stored, is not something the server can be asked about.
      if (!(ride.file instanceof Blob)) {
        throw new Error('The saved copy of this route cannot be re-sent to the '
          + 'server. Load the GPX file again.');
      }

      const payload = await postRoute(ride.file, ride.filename);
      state.routeId = payload.id;
      await store.patchRide(state.rideKey, {
        serverRouteId: payload.id,
        route: payload.route,
      });
      return true;
    })()
      .catch((err) => {
        // Swallowing this was the actual bug on the owner's box: the server
        // had forgotten the route, re-sending it failed, and every layer fell
        // back to last-saved with nothing to say why. Stale data with no
        // explanation is the one outcome this app is not allowed to produce.
        state.recoveryError = err.message;
        console.warn('Could not restore this route on the server:', err);
        return false;
      })
      .finally(() => { recovery = null; });
  }
  return recovery;
}

/** GET against the current route, recovering once from a forgotten route id. */
async function routeFetch(path, options = {}) {
  let response = await fetch(`/api/routes/${state.routeId}${path}`, options);
  if (response.status === 404 && state.rideKey && await recoverRouteId()) {
    response = await fetch(`/api/routes/${state.routeId}${path}`, options);
  }
  return response;
}

// ------------------------------------------------------------- plan controls

function wirePlan() {
  const speed = $('speed');
  const tank = $('tank');

  speed.addEventListener('input', () => { $('speed-value').textContent = speed.value; });
  speed.addEventListener('change', refreshLiveData);
  tank.addEventListener('input', () => { $('tank-value').textContent = tank.value; });
  tank.addEventListener('change', refreshLiveData);
  $('departure').addEventListener('change', refreshLiveData);
  $('refresh').addEventListener('click', refreshLiveData);
  $('export').addEventListener('click', downloadEnriched);

  // Default the departure box to the next full hour, in local time — what a
  // datetime-local input expects, and usually the right answer anyway.
  const next = new Date(Date.now() + 3600e3);
  next.setMinutes(0, 0, 0);
  const pad = (n) => String(n).padStart(2, '0');
  $('departure').value =
    `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}`
    + `T${pad(next.getHours())}:${pad(next.getMinutes())}`;
}

/** The datetime-local input carries no timezone; send the browser's offset so
 *  the server never has to guess which 08:00 was meant. */
function departureIso() {
  const raw = $('departure').value;
  if (!raw) return '';
  const local = new Date(raw);
  return Number.isNaN(local.getTime()) ? '' : local.toISOString();
}

function planParams() {
  return {
    speed: $('speed').value,
    tank: $('tank').value,
    departure: encodeURIComponent(departureIso()),
  };
}

// ------------------------------------------------------------- live data load

async function refreshLiveData() {
  if (!state.routeId && !state.rideKey) return;

  // A slider drag fires repeatedly; abandon the previous round rather than
  // letting a stale response overwrite a newer one.
  if (state.inFlight) state.inFlight.abort();
  const controller = new AbortController();
  state.inFlight = controller;

  const button = $('refresh');
  button.disabled = true;
  button.textContent = 'Loading…';

  const { speed, tank, departure } = planParams();
  const paths = {
    weather: `/weather?speed_kmh=${speed}&departure=${departure}`,
    pois: `/pois?tank_range_km=${tank}`,
    hazards: '/hazards',
    incidents: '/incidents',
    alternates: '/alternates',
  };

  Object.keys(paths).forEach(markPending);

  const outcomes = await Promise.all(
    Object.entries(paths).map(([name, path]) => loadLayer(name, path, controller.signal)),
  );

  if (controller.signal.aborted) return;

  if (state.rideKey) await store.patchRide(state.rideKey, { plan: currentPlan() });
  await refreshSavedList();

  const fromCache = outcomes.filter((outcome) => outcome === 'cache').length;
  const live = outcomes.filter((outcome) => outcome === 'live').length;
  if (live && !fromCache) state.recoveryError = null;
  updateBanner({ fromCache, live });

  if (state.inFlight === controller) state.inFlight = null;
  button.disabled = false;
  button.textContent = 'Refresh live data';
}

/**
 * Fetch one live layer, falling back to what was last stored for it.
 *
 * Returns 'live', 'cache' or 'failed' so the caller can say honestly whether
 * the sidebar is showing today's answer or last night's.
 */
async function loadLayer(name, path, signal) {
  const { panel, list } = LAYERS[name];
  const render = RENDERERS[name];

  try {
    const response = await routeFetch(path, { signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Request failed');

    render(payload);
    clearPending(name);
    if (state.rideKey) await store.saveLayer(state.rideKey, name, payload);
    return 'live';
  } catch (err) {
    if (err.name === 'AbortError') return 'aborted';
    clearPending(name);

    const cached = await cachedLayer(name);
    if (cached) {
      render(cached.payload);
      return 'cache';
    }

    $(panel).hidden = false;
    $(list).innerHTML = `<li class="muted tiny">${err.message}</li>`;
    return 'failed';
  }
}

/** A stored layer response, tagged so the panels can show it as not-live. */
async function cachedLayer(name) {
  if (!state.rideKey || !store.supported()) return null;
  try {
    const ride = await store.getRide(state.rideKey);
    const payload = ride && ride.layers ? ride.layers[name] : null;
    if (!payload) return null;
    // Reuse the vocabulary the services already speak, rather than inventing a
    // second way of saying "this is old".
    return {
      payload: { ...payload, stale: true, cached_at: ride.refreshedAt },
      refreshedAt: ride.refreshedAt,
    };
  } catch {
    return null;
  }
}

const RENDERERS = {
  weather: (payload) => panels.showWeather(payload),
  pois: (payload) => {
    state.poiPayload = payload;
    panels.showPois(payload, state.poiFilter);
  },
  hazards: (payload) => panels.showHazards(payload),
  incidents: (payload) => panels.showIncidents(payload),
  alternates: (payload) => panels.showAlternates(payload),
};

// ------------------------------------------------------------------- filters

function wirePoiFilters() {
  document.querySelectorAll('.chip[data-poi]').forEach((chip) => {
    chip.dataset.label = chip.textContent.trim().replace(/^\S+\s*/, '');
    chip.addEventListener('click', () => {
      const category = chip.dataset.poi;
      if (state.poiFilter.has(category)) state.poiFilter.delete(category);
      else state.poiFilter.add(category);
      if (state.poiPayload) panels.showPois(state.poiPayload, state.poiFilter);
    });
  });
}

// ----------------------------------------------------------------- curviness

async function loadCurviness() {
  try {
    const response = await routeFetch('/curviness');
    const payload = await response.json();
    panels.showCurviness(payload);
    rememberDemanding(payload);
    if (state.rideKey) await store.saveLayer(state.rideKey, 'curviness', payload);
  } catch {
    const cached = await cachedLayer('curviness');
    if (cached) {
      panels.showCurviness(cached.payload);
      rememberDemanding(cached.payload);
    }
    /* Otherwise: the heat map is a nicety, and its absence needs no announcement. */
  }
}

/** Keep the demanding stretches, and put them on the profile if it is drawn. */
function rememberDemanding(payload) {
  state.demanding = (payload && payload.demanding) || [];
  if (state.profile && state.profile.length) drawProfile(state.profile);
}


function wireCurvinessToggle() {
  const button = $('curviness-toggle');
  const legend = $('curviness-legend');
  legend.querySelector('.legend-bar').style.background = curvinessGradient();

  button.addEventListener('click', () => {
    state.curvinessOn = !state.curvinessOn;
    mapview.setCurvinessMode(state.curvinessOn);
    button.classList.toggle('active', state.curvinessOn);
    button.textContent = state.curvinessOn ? 'Plain route' : 'Colour by corners';
    legend.hidden = !state.curvinessOn;
  });
}

// -------------------------------------------------------------------- export

async function downloadEnriched() {
  if (!state.routeId && !state.rideKey) return;
  const { speed, tank, departure } = planParams();
  const button = $('export');
  button.disabled = true;

  try {
    // Fetched rather than navigated to, so it goes through the same recovery as
    // every other call: a plain navigation to a forgotten route id would land
    // the rider on a raw 404 page with no way back.
    const response = await routeFetch(
      `/export.gpx?speed_kmh=${speed}&tank_range_km=${tank}&departure=${departure}`,
    );
    if (!response.ok) throw new Error('Export failed — is the server reachable?');

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filenameFrom(response) || 'route_enriched.gpx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Revoking immediately can cancel the download in some browsers.
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (err) {
    panels.showError(err.message);
  } finally {
    button.disabled = false;
  }
}

/** Pull the server's suggested filename out of Content-Disposition. */
function filenameFrom(response) {
  const header = response.headers.get('content-disposition') || '';
  const match = /filename="?([^";]+)"?/.exec(header);
  return match ? match[1] : '';
}

// ------------------------------------------------------------- offline tiles

function wireOffline() {
  const status = $('offline-status');
  const cacheButton = $('cache-tiles');

  if (!tiles.supported()) {
    status.textContent =
      'Offline caching needs a secure context — it works on localhost, but not '
      + 'over plain http from another device.';
    cacheButton.disabled = true;
    $('clear-tiles').hidden = true;
    return;
  }

  tiles.onMessage((message) => {
    if (message.type === 'TILE_STATS') {
      status.textContent = message.count
        ? `${message.count} tiles cached for offline use.`
        : 'No tiles cached yet.';
    } else if (message.type === 'PREFETCH_PROGRESS') {
      status.textContent = `Caching tiles… ${message.done}/${message.total}`;
    } else if (message.type === 'PREFETCH_DONE') {
      status.textContent =
        `Cached ${message.stored} tiles`
        + (message.failed ? ` (${message.failed} failed)` : '') + '.';
      cacheButton.disabled = false;
      cacheButton.textContent = 'Cache tiles for this route';
    } else if (message.type === 'TILES_CLEARED') {
      status.textContent = 'Cached tiles cleared.';
    }
  });

  tiles.register().then((reg) => {
    if (!reg) {
      const why = tiles.registrationError();
      status.textContent = 'Offline caching unavailable'
        + (why ? `: ${why}` : ' in this browser.');
      cacheButton.disabled = true;
      return;
    }
    if (!tiles.controlling()) {
      // Registered but not controlling this page yet. Saying so beats leaving
      // "Checking…" on screen indefinitely, which reads as a hang.
      status.textContent = 'Offline caching will be ready after a reload.';
      cacheButton.disabled = true;
      return;
    }
    tiles.requestStats();
  });

  cacheButton.addEventListener('click', () => {
    const latlngs = mapview.routeLatLngs();
    if (!latlngs.length) return;

    const { tiles: wanted, zooms, skipped, partial } = tiles.tilesForRoute(
      latlngs, state.config.max_cached_tiles,
    );
    if (!wanted.length) {
      status.textContent = 'Nothing to cache for this route.';
      return;
    }
    const template = mapview.activeTileTemplate().url;
    const urls = wanted.map((tile) => tiles.tileUrl(template, tile));

    if (!tiles.prefetch(urls, state.config.tile_prefetch_delay_ms)) {
      status.textContent = 'Reload the page once, then try again — '
        + 'the offline worker is not controlling this tab yet.';
      return;
    }

    cacheButton.disabled = true;
    cacheButton.textContent = 'Caching…';
    $('tile-note').textContent =
      `Zoom ${zooms.join(', ')} along the route`
      + (partial ? ' (partial — the tile limit is too small for full coverage)' : '')
      + (skipped.length ? ` · zoom ${skipped.join(', ')} skipped to stay within the ${state.config.max_cached_tiles}-tile limit` : '')
      + '. Tiles are fetched slowly on purpose: OpenStreetMap asks clients not to bulk download.';
  });

  $('clear-tiles').addEventListener('click', () => {
    tiles.clearTiles();
    $('tile-note').textContent = '';
  });
}

// --------------------------------------------------------- elevation profile

async function loadElevation() {
  try {
    const response = await routeFetch('/elevation');
    const payload = await response.json();
    showProfile(payload);
    if (state.rideKey) await store.saveLayer(state.rideKey, 'elevation', payload);
  } catch {
    const cached = await cachedLayer('elevation');
    showProfile(cached ? cached.payload : null);
  }
}

function showProfile(payload) {
  const wrap = $('profile-wrap');
  if (!payload || !payload.available || payload.samples.length < 2) {
    wrap.hidden = true;
    return;
  }
  state.profile = payload.samples;
  wrap.hidden = false;
  drawProfile(payload.samples);
}

/* Hand-rolled SVG rather than a charting library: it keeps the page
 * dependency-free, and the shape being drawn is simple.
 *
 * The line is coloured by gradient, which is the whole point of having one.
 * A route's profile drawn in a single colour tells you the shape of the hills
 * but not where the work is -- and a 6% drag and a 6% descent look identical
 * on it, which are not remotely the same ride.
 *
 * Gradient is a polarity: climbing one way, descending the other, flat in the
 * middle. So the scale is diverging -- two hues with a neutral grey midpoint,
 * never a rainbow -- and it deliberately avoids the amber and green the map
 * already spends on curviness, so the two encodings are not confused.
 * Validated for colour-vision deficiency against the panel background rather
 * than chosen by eye.
 */
const GRADIENT_BANDS = [
  { upTo: -6, colour: '#2b6cb0', label: 'steep descent' },
  { upTo: -2, colour: '#7fcdff', label: 'descent' },
  { upTo: 2, colour: '#8a93a3', label: 'level' },
  { upTo: 6, colour: '#ffa06b', label: 'climb' },
  { upTo: Infinity, colour: '#e8452f', label: 'steep climb' },
];

function gradientBand(pct) {
  return GRADIENT_BANDS.find((b) => (pct || 0) < b.upTo) || GRADIENT_BANDS[4];
}

/** Round to something a person would put on an axis. */
function niceStep(span) {
  const rough = span / 3;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  return [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= rough) || rough;
}

function drawProfile(samples) {
  const svg = $('profile');
  const width = svg.clientWidth || 800;
  const height = svg.clientHeight || 110;
  const padL = 38;     // room for the metre labels
  const padR = 8;
  const padT = 8;
  const padB = 16;     // room for the kilometre labels

  const maxDistance = samples[samples.length - 1].distance_m || 1;
  const elevations = samples.map((s) => s.ele);
  const minEle = Math.min(...elevations);
  const maxEle = Math.max(...elevations);

  // Grid lines land on round heights rather than on the data's extremes, so
  // the eye has something regular to measure differences against -- which is
  // what the profile is for.
  const step = niceStep(Math.max(maxEle - minEle, 1));
  const gridLow = Math.floor(minEle / step) * step;
  const gridHigh = Math.ceil(maxEle / step) * step;
  const span = Math.max(gridHigh - gridLow, 1);

  const x = (d) => padL + (d / maxDistance) * (width - padL - padR);
  const y = (e) => height - padB - ((e - gridLow) / span) * (height - padT - padB);

  const parts = [];

  // Twisty and steep together, shaded behind everything else. An annotation on
  // the plot rather than another series, so it wears amber -- which the
  // gradient ramp deliberately leaves free -- and sits under the line instead
  // of competing with it. The panel below lists them; this says where they are.
  const demanding = state.demanding || [];
  demanding.forEach((stretch) => {
    const left = x(stretch.from_m);
    const right = Math.max(x(stretch.to_m), left + 2);
    parts.push(
      `<rect x="${left.toFixed(1)}" y="${padT}" width="${(right - left).toFixed(1)}" `
      + `height="${height - padT - padB}" fill="#ffc74a16"/>`
      + `<rect x="${left.toFixed(1)}" y="${height - padB - 2}" `
      + `width="${(right - left).toFixed(1)}" height="2" fill="#ffc74a"/>`);
  });

  for (let e = gridLow; e <= gridHigh + 0.001; e += step) {
    parts.push(
      `<line x1="${padL}" y1="${y(e).toFixed(1)}" x2="${width - padR}" `
      + `y2="${y(e).toFixed(1)}" stroke="#2e343f" stroke-width="1"/>`
      + `<text x="${padL - 6}" y="${(y(e) + 3.5).toFixed(1)}" fill="#9aa3b2" `
      + `font-size="10" text-anchor="end">${Math.round(e)}</text>`);
  }

  const path = samples
    .map((s, i) => `${i ? 'L' : 'M'}${x(s.distance_m).toFixed(1)},${y(s.ele).toFixed(1)}`)
    .join('');
  parts.push(`<path d="${path}L${x(maxDistance).toFixed(1)},${height - padB}`
             + `L${padL},${height - padB}Z" fill="#ffffff0d"/>`);

  // Consecutive samples in the same band become one path, so a 300-sample
  // route draws a handful of strokes instead of three hundred.
  const runs = [];
  samples.forEach((sample, i) => {
    if (i === 0) return;
    const band = gradientBand(sample.gradient_pct);
    const last = runs[runs.length - 1];
    if (last && last.band === band) last.points.push(sample);
    else runs.push({ band, points: [samples[i - 1], sample] });
  });
  runs.forEach((run) => {
    const d = run.points
      .map((s, i) => `${i ? 'L' : 'M'}${x(s.distance_m).toFixed(1)},${y(s.ele).toFixed(1)}`)
      .join('');
    parts.push(`<path d="${d}" fill="none" stroke="${run.band.colour}" `
               + `stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`);
  });

  const kmStep = niceStep(maxDistance / 1000) * 1000;
  for (let d = 0; d <= maxDistance + 1; d += kmStep) {
    parts.push(`<text x="${x(d).toFixed(1)}" y="${height - 4}" fill="#9aa3b2" `
               + `font-size="10" text-anchor="middle">${Math.round(d / 1000)}</text>`);
  }

  // The crosshair, hidden until pointed at.
  parts.push('<line id="profile-cross" x1="0" y1="' + padT + '" x2="0" y2="'
             + (height - padB) + '" stroke="#e6e9ef" stroke-width="1" '
             + 'stroke-dasharray="2 2" opacity="0"/>'
             + '<circle id="profile-dot" r="4.5" fill="#e6e9ef" stroke="#14171c" '
             + 'stroke-width="2" opacity="0"/>');

  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.innerHTML = parts.join('');

  $('profile-legend').innerHTML = GRADIENT_BANDS
    .map((b) => `<span class="swatch" style="background:${b.colour}" `
                + `title="${b.label}"></span>`).join('')
    + '<span class="tiny muted">&minus;6% &middot; level &middot; +6%</span>'
    + (demanding.length
        ? '<span class="swatch demanding-key" title="Twisty and steep"></span>'
          + '<span class="tiny muted">twisty &amp; steep</span>'
        : '');

  const cross = svg.querySelector('#profile-cross');
  const dot = svg.querySelector('#profile-dot');

  const at = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const fraction = (clientX - rect.left) / rect.width;
    const target = Math.max(0, Math.min(1, fraction)) * maxDistance;
    // Samples are ordered by distance, so a linear scan finds the nearest.
    return samples.reduce((best, s) => (
      Math.abs(s.distance_m - target) < Math.abs(best.distance_m - target) ? s : best));
  };

  // Pointer events rather than mouse events: this is read on an iPad as often
  // as on a laptop, and `mousemove` never fires there. `touch-action: none` in
  // the stylesheet stops a drag along the profile scrolling the page instead.
  const track = (event) => {
    const sample = at(event.clientX);
    const slope = sample.gradient_pct || 0;
    const band = gradientBand(slope);
    cross.setAttribute('x1', x(sample.distance_m).toFixed(1));
    cross.setAttribute('x2', x(sample.distance_m).toFixed(1));
    cross.setAttribute('opacity', '0.5');
    dot.setAttribute('cx', x(sample.distance_m).toFixed(1));
    dot.setAttribute('cy', y(sample.ele).toFixed(1));
    dot.setAttribute('fill', band.colour);
    dot.setAttribute('opacity', '1');
    const inside = demanding.some((d) => sample.distance_m >= d.from_m
                                      && sample.distance_m <= d.to_m);
    $('profile-readout').innerHTML =
      `km ${(sample.distance_m / 1000).toFixed(1)} · <strong>${Math.round(sample.ele)} m</strong>`
      + ` · ${slope > 0 ? '+' : ''}${slope.toFixed(1)}%`
      + (inside ? ' · <span class="demanding-flag">twisty &amp; steep</span>' : '');
    mapview.showPositionAt(sample.distance_m);
  };

  svg.onpointermove = track;
  svg.onpointerdown = track;
  svg.onpointerleave = (event) => {
    // A lifted finger is not a pointer that left. On touch, `pointerleave`
    // fires the instant the tap ends, so clearing here wiped the reading the
    // tap had just produced -- the profile looked inert on an iPad while
    // working perfectly under a mouse. Keeping it is also the better
    // behaviour: you tap a spot, and it stays until you tap another.
    if (event.pointerType === 'touch') return;
    cross.setAttribute('opacity', '0');
    dot.setAttribute('opacity', '0');
    $('profile-readout').textContent = '';
    mapview.hidePosition();
  };
}

// Redrawn on resize: the SVG is sized in pixels from its container, so without
// this a rotated iPad stretches the marks and the labels with them.
let profileResize;
window.addEventListener('resize', () => {
  clearTimeout(profileResize);
  profileResize = setTimeout(() => {
    if (state.profile && state.profile.length) drawProfile(state.profile);
  }, 150);
});

// ------------------------------------------------------- saved rides & restore

function currentPlan() {
  return {
    speed: $('speed').value,
    tank: $('tank').value,
    departure: $('departure').value,
  };
}

function restorePlan(plan) {
  if (!plan) return;
  if (plan.speed) {
    $('speed').value = plan.speed;
    $('speed-value').textContent = $('speed').value;
  }
  if (plan.tank) {
    $('tank').value = plan.tank;
    $('tank-value').textContent = $('tank').value;
  }
  // Only restore a departure still in the future. Reopening tomorrow morning
  // with yesterday's 08:00 silently loaded would plan the ride into the past.
  if (plan.departure && new Date(plan.departure).getTime() > Date.now()) {
    $('departure').value = plan.departure;
  }
}

/**
 * Put the last ride back on screen at start-up, before any network call.
 *
 * This is the whole point of the feature: the app comes back with your route
 * drawn whether or not there is any signal, and the banner says which of what
 * you are looking at is live.
 */
async function restoreLastRide() {
  if (!store.supported()) return false;

  let ride;
  try {
    ride = await store.mostRecent();
  } catch {
    return false;
  }
  if (!ride || !ride.route) return false;

  await openRide(ride);
  return true;
}

async function openRide(ride) {
  state.rideKey = ride.key;
  state.routeId = ride.serverRouteId;
  state.servedFromCache = true;

  showRoute(ride.route);
  restorePlan(ride.plan);

  // Draw everything that was stored, immediately and without the network, then
  // let the refresh below replace whatever it can with something live.
  const layers = ride.layers || {};
  for (const [name, payload] of Object.entries(layers)) {
    const stale = { ...payload, stale: true, cached_at: ride.refreshedAt };
    if (RENDERERS[name]) RENDERERS[name](stale);
    else if (name === 'curviness') panels.showCurviness(stale);
    else if (name === 'elevation') showProfile(stale);
  }

  updateBanner({ fromCache: Object.keys(layers).length, live: 0 });
  await store.touchRide(ride.key);
  await refreshSavedList();

  if (navigator.onLine) {
    loadElevation();
    loadCurviness();
    refreshLiveData();
  }
}

async function refreshSavedList() {
  if (!store.supported()) return;

  let rides = [];
  try {
    rides = await store.listSummaries();
  } catch {
    return;
  }

  const panel = $('saved-panel');
  panel.hidden = rides.length === 0;
  if (!rides.length) {
    // Clear as well as hide: leaving the old rows in a hidden container means
    // the next ride briefly renders behind a list of ones already forgotten.
    $('saved-list').innerHTML = '';
    return;
  }

  $('saved-list').innerHTML = rides.map((ride) => `
    <li class="${ride.key === state.rideKey ? 'active' : ''}" data-key="${ride.key}">
      <span class="what">
        <strong>${escapeHtml(ride.name)}</strong>
        <span class="muted tiny">${(ride.distance_m / 1000).toFixed(1)} km ·
        ${ride.layerCount} saved layers · ${ago(ride.openedAt)}</span>
      </span>
      <button type="button" class="icon-button" data-delete="${ride.key}"
              title="Forget this ride">&times;</button>
    </li>`).join('');

  $('saved-list').querySelectorAll('.what').forEach((element) => {
    element.addEventListener('click', async () => {
      const key = element.closest('li').dataset.key;
      if (key === state.rideKey) return;
      const ride = await store.getRide(key);
      if (ride) await openRide(ride);
    });
  });

  $('saved-list').querySelectorAll('[data-delete]').forEach((button) => {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      await store.deleteRide(button.dataset.delete);
      if (button.dataset.delete === state.rideKey) state.rideKey = null;
      await refreshSavedList();
    });
  });
}

/** Say plainly whether the sidebar is live, partly cached, or entirely cached. */
function updateBanner({ fromCache, live }) {
  const banner = $('cache-banner');
  const offline = !navigator.onLine;

  if (!fromCache && live) {
    // Everything refreshed. Confirm it briefly, then get out of the way.
    banner.className = 'panel banner fresh';
    banner.innerHTML = '<strong>Live data</strong>Everything on this page was '
      + 'just refreshed.';
    banner.hidden = false;
    clearTimeout(updateBanner.timer);
    updateBanner.timer = setTimeout(() => { banner.hidden = true; }, 4000);
    return;
  }

  if (!fromCache) {
    if (offline && (state.rideKey || state.routeId)) {
      banner.className = 'panel banner offline';
      banner.innerHTML = '<strong>Offline</strong>Your route, the profile and any '
        + 'cached tiles keep working. Live layers refresh when you have signal again.';
      banner.hidden = false;
      return;
    }
    banner.hidden = true;
    return;
  }

  banner.className = `panel banner ${offline ? 'offline' : ''}`;
  banner.innerHTML =
    `<strong>${offline ? 'Offline — showing your saved ride' : 'Showing saved data'}</strong>`
    + `${fromCache} layer${fromCache === 1 ? '' : 's'} could not be refreshed and `
    + `${fromCache === 1 ? 'is' : 'are'} being shown as last saved`
    + (live ? `; ${live} refreshed just now.` : '.')
    + (offline ? ' The route and map tiles work without a connection.' : '')
    // When the server has forgotten the route and re-sending it failed, that
    // is *the* reason nothing refreshed, and it is not guessable from here.
    // Saying "could not be refreshed" and stopping was what sent the owner
    // hunting through journalctl for a fault that was in the browser.
    + (!offline && state.recoveryError
        ? `<br><span class="tiny">${escapeHtml(state.recoveryError)}</span>`
        : '');
  banner.hidden = false;
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function ago(timestamp) {
  if (!timestamp) return 'never opened';
  const minutes = Math.round((Date.now() - timestamp) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function wireConnectivity() {
  // Coming back into signal should reconcile the page without being asked.
  window.addEventListener('online', () => {
    if (state.rideKey || state.routeId) refreshLiveData();
  });
  window.addEventListener('offline', () => updateBanner({ fromCache: 0, live: 0 }));
}

// ---------------------------------------------------------------------- boot

async function loadConfig() {
  try {
    const response = await fetch('/api/health');
    const payload = await response.json();
    Object.assign(state.config, payload.defaults || {});
    // Read the value back after assigning it: a range input silently clamps to
    // its own min/max, so a configured 40 km tank becomes 80 and the label
    // would otherwise disagree with the slider it is supposed to describe.
    $('speed').value = state.config.speed_kmh;
    $('speed-value').textContent = $('speed').value;
    $('tank').value = state.config.tank_range_km;
    $('tank-value').textContent = $('tank').value;
  } catch {
    /* Defaults in `state.config` already cover this. */
  }
}

window.addEventListener('resize', () => {
  if (state.profile) drawProfile(state.profile);
});

async function boot() {
  wireUpload();
  wirePlan();
  wirePoiFilters();
  wireCurvinessToggle();
  wireOffline();
  wireConnectivity();

  // Config first: it sets the slider defaults that a restored plan then
  // overrides, so doing it the other way round would discard the saved plan.
  await loadConfig();
  await refreshSavedList();
  await restoreLastRide();
}

boot();
