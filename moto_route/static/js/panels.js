/* Sidebar rendering.
 *
 * Each `show*` function takes one service response and updates both its panel
 * and the matching map layer, so the orchestrator in app.js makes a single call
 * per layer and no caller has to remember to update the map too.
 *
 * Every service answers with {available, reason, ...} rather than an HTTP error
 * when a source is down, so each renderer starts by handling the unavailable
 * case — a failed layer is a sentence in the sidebar, never a broken page.
 */

import { esc, km, localTime, scoreClass, curvinessGradient, curvinessLabel } from './format.js';
import * as mapview from './mapview.js';

const $ = (id) => document.getElementById(id);

export function showError(message) {
  const box = $('error');
  box.textContent = message || '';
  box.hidden = !message;
}

function unavailable(panelId, listId, message) {
  $(panelId).hidden = false;
  $(listId).innerHTML = `<li class="muted tiny">${esc(message || 'Unavailable')}</li>`;
}

/** Attach fly-to behaviour to every row carrying coordinates. */
function wireRows(listId, zoom = 13) {
  $(listId).querySelectorAll('li.clickable').forEach((item) => {
    item.addEventListener('click', () => {
      mapview.flyTo(item.dataset.lat, item.dataset.lon, zoom);
    });
  });
}

// -------------------------------------------------------------------- summary

export function showSummary(route) {
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
    ? 'drawn from the route (via points), not a recorded track' : '';
  $('waypoint-summary').textContent = [parts.join(' · '), source].filter(Boolean).join(' — ');
}

export function setCurvinessStat(value, label) {
  $('stat-curvy').textContent = `${Math.round(value)}°/km`;
  $('stat-curvy').title = label || '';
}

// -------------------------------------------------------------------- weather

export function showWeather(payload) {
  $('weather-panel').hidden = false;
  const verdict = $('weather-verdict');

  if (!payload.available) {
    verdict.innerHTML = `<div class="verdict">${esc(payload.reason || 'Unavailable')}</div>`;
    $('weather-list').innerHTML = '';
    mapview.layers.weather.clearLayers();
    return;
  }

  const summary = payload.summary || {};
  const worst = summary.worst_rideability ?? 100;
  verdict.innerHTML =
    `<div class="verdict ${scoreClass(worst)}">`
    + `<strong>${esc(summary.verdict || '')}</strong><br>`
    + `<span class="tiny muted">Worst stretch ${worst}/100 at km ${summary.worst_at_km ?? '?'} `
    + `(${esc(summary.worst_description || '')}) · `
    + `${summary.min_temperature_c}–${summary.max_temperature_c} °C</span>`
    + (payload.stale ? '<br><span class="tiny muted">Cached — live update failed.</span>' : '')
    + '</div>';

  $('weather-list').innerHTML = payload.points.map((point) => `
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

  wireRows('weather-list', 11);
  mapview.drawWeather(payload.points);
}

// ------------------------------------------------------------ fuel and stops

const POI_ICON = { fuel: '⛽', cafe: '☕', viewpoint: '📷' };

export function showPois(payload, visibleCategories) {
  $('fuel-panel').hidden = false;
  const planBox = $('fuel-plan');

  if (!payload.available) {
    planBox.innerHTML = '';
    unavailable('fuel-panel', 'poi-list', payload.reason);
    mapview.layers.pois.clearLayers();
    return;
  }

  const plan = payload.fuel_plan || {};
  const counts = payload.counts || {};

  // The fuel gap is the one finding here that changes what you do before
  // leaving, so it leads — and it is styled as a warning, not a statistic.
  if (plan.gaps && plan.gaps.length) {
    const worst = plan.gaps.reduce((a, b) => (b.length_km > a.length_km ? b : a));
    planBox.innerHTML =
      `<div class="verdict bad"><strong>No fuel for ${worst.length_km} km</strong><br>`
      + `<span class="tiny muted">Between km ${worst.from_km} and km ${worst.to_km}. `
      + `Usable range ${plan.usable_range_km} km`
      + (plan.gaps.length > 1 ? ` · ${plan.gaps.length} dry stretches in total` : '')
      + '</span></div>';
  } else if (plan.stop_count) {
    planBox.innerHTML =
      `<div class="verdict good"><strong>${plan.stop_count} fuel stop`
      + `${plan.stop_count === 1 ? '' : 's'} needed</strong><br>`
      + `<span class="tiny muted">Ringed on the map. Usable range ${plan.usable_range_km} km.</span></div>`;
  } else {
    planBox.innerHTML =
      '<div class="verdict good"><strong>Within one tank</strong><br>'
      + `<span class="tiny muted">Usable range ${plan.usable_range_km} km.</span></div>`;
  }

  const visible = payload.pois.filter((poi) => visibleCategories.has(poi.category));
  $('poi-list').innerHTML = visible.length
    ? visible.map((poi) => `
      <li class="clickable" data-lat="${poi.lat}" data-lon="${poi.lon}">
        <span class="where">km ${(poi.distance_along_route_m / 1000).toFixed(0)}</span>
        <span class="what">
          ${POI_ICON[poi.category] || ''} ${esc(poi.name)}
          ${poi.recommended ? '<span class="badge">planned stop</span>' : ''}
          ${poi.detail ? `<span class="warn-text">${esc(poi.detail)}</span>` : ''}
        </span>
      </li>`).join('')
    : '<li class="muted tiny">Nothing mapped in these categories along the route.</li>';

  // Update the filter chips with what was actually found.
  document.querySelectorAll('.chip[data-poi]').forEach((chip) => {
    const category = chip.dataset.poi;
    const count = counts[category] || 0;
    chip.textContent = `${POI_ICON[category]} ${chip.dataset.label || category} (${count})`;
    chip.classList.toggle('active', visibleCategories.has(category));
  });

  wireRows('poi-list', 14);
  mapview.drawPois(payload.pois, visibleCategories);
}

// -------------------------------------------------------------------- hazards

export function showHazards(payload) {
  $('hazard-panel').hidden = false;
  $('hazard-note').textContent = payload.note || payload.reason || '';

  if (!payload.available) {
    unavailable('hazard-panel', 'hazard-list', payload.reason);
    mapview.layers.hazards.clearLayers();
    return;
  }
  if (!payload.hazards.length) {
    $('hazard-list').innerHTML =
      '<li class="muted tiny">No mapped closures or roadworks on this route.</li>';
    mapview.layers.hazards.clearLayers();
    return;
  }

  const icon = { closed: '⛔', restricted: '⚠️', info: 'ℹ️' };
  $('hazard-list').innerHTML = payload.hazards.map((hazard) => `
    <li class="clickable" data-lat="${hazard.lat}" data-lon="${hazard.lon}">
      <span class="where">km ${(hazard.distance_along_route_m / 1000).toFixed(0)}</span>
      <span class="what">
        ${icon[hazard.severity] || ''} ${esc(hazard.label)}
        ${hazard.detail ? `<span class="warn-text">${esc(hazard.detail)}</span>` : ''}
      </span>
    </li>`).join('');

  wireRows('hazard-list', 14);
  mapview.drawHazards(payload.hazards);
}

// ------------------------------------------------------------------ incidents

export function showIncidents(payload) {
  $('incident-panel').hidden = false;
  const note = [payload.note || payload.reason || ''];
  if (payload.failed_sources && payload.failed_sources.length) {
    note.push(`Could not reach: ${payload.failed_sources.join(', ')}.`);
  }
  $('incident-note').textContent = note.filter(Boolean).join(' ');

  if (!payload.available) {
    unavailable('incident-panel', 'incident-list', payload.reason);
    mapview.layers.incidents.clearLayers();
    return;
  }
  if (!payload.incidents.length) {
    $('incident-list').innerHTML =
      '<li class="muted tiny">No live incidents reported on this route.</li>';
    mapview.layers.incidents.clearLayers();
    return;
  }

  const icon = { closure: '⛔', roadworks: '🚧', warning: '⚠️', incident: 'ℹ️' };
  $('incident-list').innerHTML = payload.incidents.map((incident) => `
    <li class="clickable" data-lat="${incident.lat}" data-lon="${incident.lon}">
      <span class="where">km ${(incident.distance_along_route_m / 1000).toFixed(0)}</span>
      <span class="what">
        ${icon[incident.category] || ''} ${esc(incident.title)}
        ${incident.detail ? `<span class="warn-text">${esc(incident.detail)}</span>` : ''}
        <span class="muted tiny">${esc(incident.source)}</span>
      </span>
    </li>`).join('');

  wireRows('incident-list', 14);
  mapview.drawIncidents(payload.incidents);
}

// ----------------------------------------------------------------- alternates

export function showAlternates(payload) {
  $('alternate-panel').hidden = false;
  $('alternate-note').textContent = payload.note || payload.reason || '';

  if (payload.original) {
    setCurvinessStat(payload.original.curviness, curvinessLabel(payload.original.curviness));
  }

  if (!payload.available || !payload.alternates.length) {
    unavailable('alternate-panel', 'alternate-list', payload.reason || 'No alternates found.');
    mapview.layers.alternates.clearLayers();
    return;
  }

  const { colours, lines } = mapview.drawAlternates(payload.alternates);

  $('alternate-list').innerHTML = payload.alternates.map((alt, index) => `
    <li class="clickable" data-index="${index}">
      <span class="where" style="color:${colours[index % colours.length]}">■</span>
      <span class="what">
        <strong>${esc(alt.label)}</strong>
        <span class="warn-text">${alt.distance_km} km · ${alt.duration_min} min ·
        ${alt.curviness}°/km of corners</span>
      </span>
    </li>`).join('');

  $('alternate-list').querySelectorAll('li.clickable').forEach((item) => {
    item.addEventListener('click', () => {
      mapview.focusAlternate(lines[Number(item.dataset.index)]);
    });
  });
}

// ------------------------------------------------------------------ curviness

export function showCurviness(payload) {
  if (!payload.available || !payload.samples.length) return;
  mapview.drawCurviness(payload.samples);
  setCurvinessStat(payload.overall, curvinessLabel(payload.overall));

  const bar = document.querySelector('#curviness-legend .legend-bar');
  if (bar) bar.style.background = curvinessGradient();
}
