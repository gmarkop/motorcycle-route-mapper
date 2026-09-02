/* Motorcycle Route Mapper — browser side.
 *
 * Deliberately dependency-free apart from Leaflet: no build step, no bundler,
 * no framework. The whole UI is small enough to read in one sitting, which is
 * the point of a tool you maintain yourself.
 *
 * The flow is: upload a file -> the server returns the parsed route -> draw it
 * -> then fire the three live layers independently so a slow one never blocks
 * the map.
 */

const state = {
  routeId: null,
  route: null,
  layers: {},          // Leaflet layer groups, so each can be cleared alone
  profile: null,       // elevation samples
  inFlight: null,      // AbortController for the current live-data refresh
};

// ---------------------------------------------------------------- map set-up

const map = L.map('map', { zoomControl: true }).setView([47.0, 10.5], 6);

const baseLayers = {
  'OpenStreetMap': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }),
  // Contours and hillshading: worth the extra tiles when the route is alpine.
  'Topographic': L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
    maxZoom: 17,
    attribution: 'Map data &copy; OpenStreetMap contributors, SRTM | Tiles &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
  }),
};
baseLayers['OpenStreetMap'].addTo(map);

['route', 'waypoints', 'weather', 'hazards', 'alternates'].forEach((name) => {
  state.layers[name] = L.layerGroup().addTo(map);
});

L.control.layers(baseLayers, {
  'Route': state.layers.route,
  'Waypoints': state.layers.waypoints,
  'Weather': state.layers.weather,
  'Closures': state.layers.hazards,
  'Alternates': state.layers.alternates,
}, { collapsed: false }).addTo(map);

const positionMarker = L.circleMarker([0, 0], {
  radius: 6, color: '#fff', weight: 2, fillColor: '#ff7a2f', fillOpacity: 1,
});

// ------------------------------------------------------------------- helpers

const $ = (id) => document.getElementById(id);

/** Escape text before it goes into innerHTML. Route files are user data and a
 *  waypoint name can legitimately contain "<". */
function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

const km = (metres) => `${(metres / 1000).toFixed(1)} km`;

function localTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function scoreClass(score) {
  if (score >= 75) return 'good';
  if (score >= 45) return 'mid';
  return 'bad';
}

function scoreColour(score) {
  if (score >= 75) return '#4ec97f';
  if (score >= 45) return '#ffc74a';
  return '#ff6b5e';
}

function showError(message) {
  const box = $('error');
  box.textContent = message;
  box.hidden = !message;
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
  showError('');
  const body = new FormData();
  body.append('file', file);

  try {
    const response = await fetch('/api/routes', { method: 'POST', body });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Upload failed.');

    state.routeId = payload.id;
    state.route = payload.route;
    drawRoute(payload.route);
    $('plan').hidden = false;
    loadElevation();
    refreshLiveData();
  } catch (err) {
    showError(err.message);
  }
}

// ------------------------------------------------------------ drawing the ride

function drawRoute(route) {
  Object.values(state.layers).forEach((layer) => layer.clearLayers());

  const lines = route.lines.map((line) => line.map((p) => [p.lat, p.lon]));
  lines.forEach((coords) => {
    // A casing underneath makes the route readable over both light map tiles
    // and dark forest, which a single stroke never manages.
    L.polyline(coords, { color: '#00000088', weight: 8 }).addTo(state.layers.route);
    L.polyline(coords, { color: '#ff7a2f', weight: 4 }).addTo(state.layers.route);
  });

  if (lines.length && lines[0].length) {
    L.marker(lines[0][0], { title: 'Start' })
      .bindPopup('<b>Start</b>').addTo(state.layers.waypoints);
    const lastLine = lines[lines.length - 1];
    L.marker(lastLine[lastLine.length - 1], { title: 'Finish' })
      .bindPopup('<b>Finish</b>').addTo(state.layers.waypoints);
  }

  route.waypoints.forEach((wp) => {
    // Shaping points only bend the line onto a road; drawing them the same size
    // as a real stop would bury the useful markers in noise.
    const shaping = wp.kind === 'shaping';
    L.circleMarker([wp.lat, wp.lon], {
      radius: shaping ? 3 : 7,
      color: shaping ? '#7d8698' : '#ffffff',
      weight: shaping ? 1 : 2,
      fillColor: shaping ? '#7d8698' : '#4ea8ff',
      fillOpacity: 1,
    }).bindPopup(
      `<b>${esc(wp.name || (shaping ? 'Shaping point' : 'Waypoint'))}</b>` +
      (wp.description ? `<br>${esc(wp.description)}` : '') +
      (wp.distance_m != null ? `<br><small>km ${(wp.distance_m / 1000).toFixed(1)}</small>` : '')
    ).addTo(state.layers.waypoints);
  });

  if (route.bounds) {
    const [minLat, minLon, maxLat, maxLon] = route.bounds;
    map.fitBounds([[minLat, minLon], [maxLat, maxLon]], { padding: [30, 30] });
  }

  renderSummary(route);
}

function renderSummary(route) {
  $('summary').hidden = false;
  $('route-name').textContent = route.name || 'Route';
  $('stat-distance').textContent = km(route.stats.distance_m);
  $('stat-ascent').textContent = route.stats.ascent_m ? `${route.stats.ascent_m} m` : '–';
  $('stat-points').textContent = route.stats.point_count.toLocaleString();
  $('stat-curvy').textContent = '…';

  const kinds = route.waypoints.reduce((acc, wp) => {
    acc[wp.kind] = (acc[wp.kind] || 0) + 1;
    return acc;
  }, {});
  const parts = [];
  if (kinds.via) parts.push(`${kinds.via} via points`);
  if (kinds.waypoint) parts.push(`${kinds.waypoint} waypoints`);
  if (kinds.shaping) parts.push(`${kinds.shaping} shaping points`);
  const source = route.metadata.drawn_from === 'route'
    ? 'drawn from the route (via points), not a recorded track'
    : '';
  $('waypoint-summary').textContent = [parts.join(' · '), source].filter(Boolean).join(' — ');
}

// ------------------------------------------------------------- live data load

function wirePlan() {
  const speed = $('speed');
  speed.addEventListener('input', () => { $('speed-value').textContent = speed.value; });
  speed.addEventListener('change', refreshLiveData);
  $('departure').addEventListener('change', refreshLiveData);
  $('refresh').addEventListener('click', refreshLiveData);

  // Default the departure box to the next full hour, in local time — that is
  // what a datetime-local input expects, and it is the usual answer anyway.
  const next = new Date(Date.now() + 3600e3);
  next.setMinutes(0, 0, 0);
  const pad = (n) => String(n).padStart(2, '0');
  $('departure').value =
    `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}` +
    `T${pad(next.getHours())}:${pad(next.getMinutes())}`;
}

/** The datetime-local input has no timezone; send the browser's offset so the
 *  server does not have to guess which 08:00 was meant. */
function departureIso() {
  const raw = $('departure').value;
  if (!raw) return '';
  const local = new Date(raw);
  return Number.isNaN(local.getTime()) ? '' : local.toISOString();
}

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

  const speed = $('speed').value;
  const departure = encodeURIComponent(departureIso());
  const base = `/api/routes/${state.routeId}`;

  const request = async (path, render, panelId) => {
    try {
      const response = await fetch(path, { signal: controller.signal });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Request failed');
      render(payload);
    } catch (err) {
      if (err.name === 'AbortError') return;
      renderUnavailable(panelId, err.message);
    }
  };

  await Promise.all([
    request(`${base}/weather?speed_kmh=${speed}&departure=${departure}`,
            renderWeather, 'weather-panel'),
    request(`${base}/hazards`, renderHazards, 'hazard-panel'),
    request(`${base}/alternates`, renderAlternates, 'alternate-panel'),
  ]);

  if (state.inFlight === controller) state.inFlight = null;
  button.disabled = false;
  button.textContent = 'Refresh live data';
}

function renderUnavailable(panelId, message) {
  const panel = $(panelId);
  panel.hidden = false;
  const list = panel.querySelector('.list');
  if (list) list.innerHTML = `<li class="muted tiny">${esc(message)}</li>`;
}

// ----------------------------------------------------------------- weather UI

function renderWeather(payload) {
  const panel = $('weather-panel');
  panel.hidden = false;
  state.layers.weather.clearLayers();

  const list = $('weather-list');
  const verdict = $('weather-verdict');

  if (!payload.available) {
    verdict.innerHTML = `<div class="verdict">${esc(payload.reason || 'Unavailable')}</div>`;
    list.innerHTML = '';
    return;
  }

  const summary = payload.summary || {};
  const worst = summary.worst_rideability ?? 100;
  verdict.innerHTML =
    `<div class="verdict ${scoreClass(worst)}">` +
    `<strong>${esc(summary.verdict || '')}</strong><br>` +
    `<span class="tiny muted">Worst stretch ${worst}/100 at km ${summary.worst_at_km ?? '?'} ` +
    `(${esc(summary.worst_description || '')}) · ` +
    `${summary.min_temperature_c}–${summary.max_temperature_c} °C</span>` +
    (payload.stale ? '<br><span class="tiny muted">Cached — live update failed.</span>' : '') +
    '</div>';

  list.innerHTML = payload.points.map((point) => `
    <li class="clickable" data-lat="${point.lat}" data-lon="${point.lon}">
      <span class="where">km ${(point.distance_m / 1000).toFixed(0)}<br>${localTime(point.eta)}</span>
      <span class="what">
        ${esc(point.description)}
        ${point.temperature_c != null ? `· ${point.temperature_c.toFixed(0)} °C` : ''}
        ${point.gust_kmh != null ? `· gusts ${point.gust_kmh.toFixed(0)} km/h` : ''}
        ${point.warnings.map((w) => `<span class="warn-text">${esc(w)}</span>`).join('')}
      </span>
      <span class="score ${scoreClass(point.rideability)}">${point.rideability}</span>
    </li>`).join('');

  list.querySelectorAll('li.clickable').forEach((item) => {
    item.addEventListener('click', () => {
      map.flyTo([Number(item.dataset.lat), Number(item.dataset.lon)], 11);
    });
  });

  payload.points.forEach((point) => {
    L.circleMarker([point.lat, point.lon], {
      radius: 9,
      color: '#14171c',
      weight: 2,
      fillColor: scoreColour(point.rideability),
      fillOpacity: 0.95,
    }).bindPopup(
      `<b>km ${(point.distance_m / 1000).toFixed(0)} — ${localTime(point.eta)}</b><br>` +
      `${esc(point.description)}<br>` +
      `${point.temperature_c ?? '?'} °C (feels ${point.apparent_c ?? '?'})<br>` +
      `Wind ${point.wind_kmh ?? '?'} km/h, gusts ${point.gust_kmh ?? '?'} km/h<br>` +
      `Rain ${point.precipitation_mm ?? 0} mm/h (${point.precipitation_probability ?? 0}%)<br>` +
      `<b>Rideability ${point.rideability}/100</b>` +
      (point.warnings.length ? `<br>${point.warnings.map(esc).join('<br>')}` : '')
    ).addTo(state.layers.weather);
  });
}

// ----------------------------------------------------------------- hazards UI

function renderHazards(payload) {
  const panel = $('hazard-panel');
  panel.hidden = false;
  state.layers.hazards.clearLayers();

  const list = $('hazard-list');
  $('hazard-note').textContent = payload.note || payload.reason || '';

  if (!payload.available) {
    list.innerHTML = `<li class="muted tiny">${esc(payload.reason || 'Unavailable')}</li>`;
    return;
  }
  if (!payload.hazards.length) {
    list.innerHTML = '<li class="muted tiny">No mapped closures or roadworks on this route.</li>';
    return;
  }

  const icon = { closed: '⛔', restricted: '⚠️', info: 'ℹ️' };

  list.innerHTML = payload.hazards.map((hazard) => `
    <li class="clickable" data-lat="${hazard.lat}" data-lon="${hazard.lon}">
      <span class="where">km ${(hazard.distance_along_route_m / 1000).toFixed(0)}</span>
      <span class="what">
        ${icon[hazard.severity] || ''} ${esc(hazard.label)}
        ${hazard.detail ? `<span class="warn-text">${esc(hazard.detail)}</span>` : ''}
      </span>
    </li>`).join('');

  list.querySelectorAll('li.clickable').forEach((item) => {
    item.addEventListener('click', () => {
      map.flyTo([Number(item.dataset.lat), Number(item.dataset.lon)], 14);
    });
  });

  payload.hazards.forEach((hazard) => {
    L.circleMarker([hazard.lat, hazard.lon], {
      radius: 8,
      color: '#14171c',
      weight: 2,
      fillColor: hazard.severity === 'closed' ? '#ff6b5e' : '#ffc74a',
      fillOpacity: 0.95,
    }).bindPopup(
      `<b>${esc(hazard.label)}</b><br>` +
      `${esc(hazard.detail)}<br>` +
      `<small>km ${(hazard.distance_along_route_m / 1000).toFixed(1)} · ` +
      `${Math.round(hazard.distance_off_route_m)} m off route</small><br>` +
      `<a href="${esc(hazard.osm_url)}" target="_blank" rel="noopener">View in OpenStreetMap</a>`
    ).addTo(state.layers.hazards);
  });
}

// -------------------------------------------------------------- alternates UI

function renderAlternates(payload) {
  const panel = $('alternate-panel');
  panel.hidden = false;
  state.layers.alternates.clearLayers();

  const list = $('alternate-list');
  $('alternate-note').textContent = payload.note || payload.reason || '';

  if (payload.original) {
    $('stat-curvy').textContent = `${Math.round(payload.original.curviness)}°/km`;
  }

  if (!payload.available || !payload.alternates.length) {
    list.innerHTML = `<li class="muted tiny">${esc(payload.reason || 'No alternates found.')}</li>`;
    return;
  }

  const colours = ['#4ea8ff', '#b48cff', '#4ec97f'];

  list.innerHTML = payload.alternates.map((alt, index) => `
    <li class="clickable" data-index="${index}">
      <span class="where" style="color:${colours[index % colours.length]}">■</span>
      <span class="what">
        <strong>${esc(alt.label)}</strong>
        <span class="warn-text">${alt.distance_km} km · ${alt.duration_min} min ·
        ${alt.curviness}°/km of corners</span>
      </span>
    </li>`).join('');

  payload.alternates.forEach((alt, index) => {
    const line = L.polyline(alt.coordinates, {
      color: colours[index % colours.length],
      weight: 4,
      opacity: 0.85,
      dashArray: '8 6',
    }).bindPopup(
      `<b>${esc(alt.label)}</b><br>${alt.distance_km} km · ${alt.duration_min} min<br>` +
      `Corners: ${alt.curviness}°/km`
    ).addTo(state.layers.alternates);
    alt._line = line;
  });

  list.querySelectorAll('li.clickable').forEach((item) => {
    item.addEventListener('click', () => {
      const line = payload.alternates[Number(item.dataset.index)]._line;
      map.fitBounds(line.getBounds(), { padding: [30, 30] });
      line.openPopup();
    });
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

/* Hand-rolled SVG rather than a charting library: it is about thirty lines,
 * and it keeps the page dependency-free. */
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

  const line = samples.map((s, i) => `${i ? 'L' : 'M'}${x(s.distance_m).toFixed(1)},${y(s.ele).toFixed(1)}`).join('');
  const area = `${line}L${x(maxDistance).toFixed(1)},${height - pad}L${pad},${height - pad}Z`;

  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.innerHTML =
    `<path d="${area}" fill="#ff7a2f22"/>` +
    `<path d="${line}" fill="none" stroke="#ff7a2f" stroke-width="1.5"/>` +
    `<text x="${pad + 2}" y="12" fill="#9aa3b2" font-size="10">${Math.round(maxEle)} m</text>` +
    `<text x="${pad + 2}" y="${height - 6}" fill="#9aa3b2" font-size="10">${Math.round(minEle)} m</text>`;

  svg.onmousemove = (event) => {
    const rect = svg.getBoundingClientRect();
    const fraction = (event.clientX - rect.left) / rect.width;
    const target = fraction * maxDistance;
    // Samples are ordered by distance, so the nearest one is a simple scan.
    const sample = samples.reduce((best, s) =>
      Math.abs(s.distance_m - target) < Math.abs(best.distance_m - target) ? s : best);
    $('profile-readout').textContent = `km ${(sample.distance_m / 1000).toFixed(1)} · ${Math.round(sample.ele)} m`;
    showPositionOnMap(sample.distance_m);
  };
  svg.onmouseleave = () => {
    $('profile-readout').textContent = '';
    positionMarker.remove();
  };
}

/** Put a marker on the map at a given distance along the route, so hovering the
 *  profile shows you which climb you are looking at. */
function showPositionOnMap(distanceM) {
  if (!state.route) return;
  const points = state.route.lines.flat();
  if (points.length < 2) return;

  // Walk the drawn line accumulating distance. Cheap enough at this size, and
  // it avoids shipping a second copy of the geometry just for the lookup.
  let travelled = 0;
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1];
    const b = points[i];
    travelled += haversine(a.lat, a.lon, b.lat, b.lon);
    if (travelled >= distanceM) {
      positionMarker.setLatLng([b.lat, b.lon]).addTo(map);
      return;
    }
  }
}

function haversine(lat1, lon1, lat2, lon2) {
  const toRad = Math.PI / 180;
  const dLat = (lat2 - lat1) * toRad;
  const dLon = (lon2 - lon1) * toRad;
  const h = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.sqrt(h));
}

window.addEventListener('resize', () => {
  if (state.profile) drawProfile(state.profile);
});

wireUpload();
wirePlan();
