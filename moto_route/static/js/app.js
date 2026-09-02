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
import { curvinessGradient } from './format.js';

const $ = (id) => document.getElementById(id);

const state = {
  routeId: null,
  profile: null,
  inFlight: null,                                   // AbortController
  poiFilter: new Set(['fuel', 'cafe', 'viewpoint']),
  poiPayload: null,
  curvinessOn: false,
  config: {
    speed_kmh: 65, tank_range_km: 250,
    max_cached_tiles: 250, tile_prefetch_delay_ms: 120,
  },
};

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
  const body = new FormData();
  body.append('file', file);

  try {
    const response = await fetch('/api/routes', { method: 'POST', body });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Upload failed.');

    state.routeId = payload.id;
    mapview.drawRoute(payload.route);
    panels.showSummary(payload.route);
    $('plan').hidden = false;
    $('offline-panel').hidden = false;

    loadElevation();
    loadCurviness();
    refreshLiveData();
  } catch (err) {
    panels.showError(err.message);
  }
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
  if (!state.routeId) return;

  // A slider drag fires repeatedly; abandon the previous round rather than
  // letting a stale response overwrite a newer one.
  if (state.inFlight) state.inFlight.abort();
  const controller = new AbortController();
  state.inFlight = controller;

  const button = $('refresh');
  button.disabled = true;
  button.textContent = 'Loading…';

  const { speed, tank, departure } = planParams();
  const base = `/api/routes/${state.routeId}`;

  const request = async (path, render, panelId, listId) => {
    try {
      const response = await fetch(path, { signal: controller.signal });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Request failed');
      render(payload);
    } catch (err) {
      if (err.name === 'AbortError') return;
      $(panelId).hidden = false;
      $(listId).innerHTML = `<li class="muted tiny">${err.message}</li>`;
    }
  };

  await Promise.all([
    request(`${base}/weather?speed_kmh=${speed}&departure=${departure}`,
            panels.showWeather, 'weather-panel', 'weather-list'),
    request(`${base}/pois?tank_range_km=${tank}`, (payload) => {
      state.poiPayload = payload;
      panels.showPois(payload, state.poiFilter);
    }, 'fuel-panel', 'poi-list'),
    request(`${base}/hazards`, panels.showHazards, 'hazard-panel', 'hazard-list'),
    request(`${base}/incidents`, panels.showIncidents, 'incident-panel', 'incident-list'),
    request(`${base}/alternates`, panels.showAlternates, 'alternate-panel', 'alternate-list'),
  ]);

  if (state.inFlight === controller) state.inFlight = null;
  button.disabled = false;
  button.textContent = 'Refresh live data';
}

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
    const response = await fetch(`/api/routes/${state.routeId}/curviness`);
    const payload = await response.json();
    panels.showCurviness(payload);
  } catch {
    /* The heat map is a nicety; its absence needs no announcement. */
  }
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

function downloadEnriched() {
  if (!state.routeId) return;
  const { speed, tank, departure } = planParams();
  // A plain navigation rather than fetch+blob: the browser then handles the
  // Content-Disposition filename, and nothing has to be held in memory.
  window.location.href =
    `/api/routes/${state.routeId}/export.gpx`
    + `?speed_kmh=${speed}&tank_range_km=${tank}&departure=${departure}`;
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
  const wrap = $('profile-wrap');
  try {
    const response = await fetch(`/api/routes/${state.routeId}/elevation`);
    const payload = await response.json();
    if (!payload.available || payload.samples.length < 2) {
      wrap.hidden = true;
      return;
    }
    state.profile = payload.samples;
    wrap.hidden = false;
    drawProfile(payload.samples);
  } catch {
    wrap.hidden = true;
  }
}

/* Hand-rolled SVG rather than a charting library: about thirty lines, and it
 * keeps the page dependency-free. */
function drawProfile(samples) {
  const svg = $('profile');
  const width = svg.clientWidth || 800;
  const height = svg.clientHeight || 100;
  const pad = 4;

  const maxDistance = samples[samples.length - 1].distance_m || 1;
  const elevations = samples.map((s) => s.ele);
  const minEle = Math.min(...elevations);
  const maxEle = Math.max(...elevations);
  const span = Math.max(maxEle - minEle, 1);

  const x = (d) => pad + (d / maxDistance) * (width - 2 * pad);
  const y = (e) => height - pad - ((e - minEle) / span) * (height - 2 * pad);

  const line = samples
    .map((s, i) => `${i ? 'L' : 'M'}${x(s.distance_m).toFixed(1)},${y(s.ele).toFixed(1)}`)
    .join('');
  const area = `${line}L${x(maxDistance).toFixed(1)},${height - pad}L${pad},${height - pad}Z`;

  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.innerHTML =
    `<path d="${area}" fill="#ff7a2f22"/>`
    + `<path d="${line}" fill="none" stroke="#ff7a2f" stroke-width="1.5"/>`
    + `<text x="${pad + 2}" y="12" fill="#9aa3b2" font-size="10">${Math.round(maxEle)} m</text>`
    + `<text x="${pad + 2}" y="${height - 6}" fill="#9aa3b2" font-size="10">${Math.round(minEle)} m</text>`;

  svg.onmousemove = (event) => {
    const rect = svg.getBoundingClientRect();
    const fraction = (event.clientX - rect.left) / rect.width;
    const target = fraction * maxDistance;
    // Samples are ordered by distance, so a linear scan finds the nearest.
    const sample = samples.reduce((best, s) => (
      Math.abs(s.distance_m - target) < Math.abs(best.distance_m - target) ? s : best));
    $('profile-readout').textContent =
      `km ${(sample.distance_m / 1000).toFixed(1)} · ${Math.round(sample.ele)} m`;
    mapview.showPositionAt(sample.distance_m);
  };
  svg.onmouseleave = () => {
    $('profile-readout').textContent = '';
    mapview.hidePosition();
  };
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

loadConfig();
wireUpload();
wirePlan();
wirePoiFilters();
wireCurvinessToggle();
wireOffline();
