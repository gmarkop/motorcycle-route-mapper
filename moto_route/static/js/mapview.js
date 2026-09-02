/* Everything that draws on the Leaflet map.
 *
 * The sidebar modules never touch Leaflet directly; they call into here. That
 * keeps the map's layer bookkeeping — which group holds what, what gets cleared
 * when a new file is loaded — in one place instead of spread across five
 * renderers.
 */

import { esc, curvinessColour, curvinessLabel, localTime } from './format.js';

export const map = L.map('map', { zoomControl: true }).setView([47.0, 10.5], 6);

export const BASE_LAYERS = {
  'OpenStreetMap': {
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    options: {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
  // Contours and hillshading, worth the extra tiles when the route is alpine.
  'Topographic': {
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    options: {
      maxZoom: 17,
      attribution: 'Map data &copy; OpenStreetMap contributors, SRTM | Tiles &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
    },
  },
};

const baseLayerObjects = {};
Object.entries(BASE_LAYERS).forEach(([name, config]) => {
  baseLayerObjects[name] = L.tileLayer(config.url, config.options);
});
baseLayerObjects['OpenStreetMap'].addTo(map);

let activeBaseName = 'OpenStreetMap';
map.on('baselayerchange', (event) => { activeBaseName = event.name; });

/** The tile template currently on screen — what the offline cache must fetch. */
export function activeTileTemplate() {
  return BASE_LAYERS[activeBaseName] || BASE_LAYERS['OpenStreetMap'];
}

export const layers = {};
['route', 'curviness', 'waypoints', 'weather', 'pois', 'hazards', 'incidents', 'alternates']
  .forEach((name) => { layers[name] = L.layerGroup(); });

// The curviness overlay starts hidden: it replaces the plain route rather than
// stacking on top of it, and two lines drawn over each other read as neither.
Object.entries(layers).forEach(([name, layer]) => {
  if (name !== 'curviness') layer.addTo(map);
});

L.control.layers(baseLayerObjects, {
  'Route': layers.route,
  'Waypoints': layers.waypoints,
  'Weather': layers.weather,
  'Fuel & stops': layers.pois,
  'Closures': layers.hazards,
  'Live incidents': layers.incidents,
  'Alternates': layers.alternates,
}, { collapsed: true }).addTo(map);

const positionMarker = L.circleMarker([0, 0], {
  radius: 6, color: '#fff', weight: 2, fillColor: '#ff7a2f', fillOpacity: 1,
});

let currentRoute = null;

export function getRoute() {
  return currentRoute;
}

/** Flat [lat, lon] list of the drawn route — used for tile prefetching. */
export function routeLatLngs() {
  if (!currentRoute) return [];
  return currentRoute.lines.flat().map((p) => [p.lat, p.lon]);
}

export function flyTo(lat, lon, zoom = 13) {
  map.flyTo([Number(lat), Number(lon)], zoom);
}

export function clearAll() {
  Object.values(layers).forEach((layer) => layer.clearLayers());
}

// --------------------------------------------------------------------- route

export function drawRoute(route) {
  currentRoute = route;
  clearAll();

  route.lines.forEach((line) => {
    const coords = line.map((p) => [p.lat, p.lon]);
    // A dark casing under the orange keeps the line readable over both pale
    // fields and dark forest, which a single stroke never manages.
    L.polyline(coords, { color: '#00000088', weight: 8 }).addTo(layers.route);
    L.polyline(coords, { color: '#ff7a2f', weight: 4 }).addTo(layers.route);
  });

  const lines = route.lines.filter((line) => line.length);
  if (lines.length) {
    const first = lines[0][0];
    const lastLine = lines[lines.length - 1];
    const last = lastLine[lastLine.length - 1];
    L.marker([first.lat, first.lon], { title: 'Start' })
      .bindPopup('<b>Start</b>').addTo(layers.waypoints);
    L.marker([last.lat, last.lon], { title: 'Finish' })
      .bindPopup('<b>Finish</b>').addTo(layers.waypoints);
  }

  route.waypoints.forEach((wp) => {
    // Shaping points only bend the line onto a road; drawn full size they would
    // bury the markers that represent actual decisions.
    const shaping = wp.kind === 'shaping';
    L.circleMarker([wp.lat, wp.lon], {
      radius: shaping ? 3 : 7,
      color: shaping ? '#7d8698' : '#ffffff',
      weight: shaping ? 1 : 2,
      fillColor: shaping ? '#7d8698' : '#4ea8ff',
      fillOpacity: 1,
    }).bindPopup(
      `<b>${esc(wp.name || (shaping ? 'Shaping point' : 'Waypoint'))}</b>`
      + (wp.description ? `<br>${esc(wp.description)}` : '')
      + (wp.distance_m != null ? `<br><small>km ${(wp.distance_m / 1000).toFixed(1)}</small>` : ''),
    ).addTo(layers.waypoints);
  });

  if (route.bounds) {
    const [minLat, minLon, maxLat, maxLon] = route.bounds;
    map.fitBounds([[minLat, minLon], [maxLat, maxLon]], { padding: [30, 30] });
  }
}

// ----------------------------------------------------------------- curviness

export function drawCurviness(samples) {
  layers.curviness.clearLayers();

  // Two passes, not one. Drawing casing-then-colour per segment lets the next
  // segment's casing paint over the previous segment's colour, which on a road
  // that doubles back — every hairpin — turns the line black. All the casing
  // goes down first, then every colour on top of it.
  for (let i = 1; i < samples.length; i += 1) {
    const a = samples[i - 1];
    const b = samples[i];
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
      color: '#0b0d10', weight: 9, opacity: 0.85,
    }).addTo(layers.curviness);
  }

  for (let i = 1; i < samples.length; i += 1) {
    const a = samples[i - 1];
    const b = samples[i];
    // Colour each segment by the mean of its ends, so the ramp is continuous
    // rather than stepping at every anchor.
    const value = (a.curviness + b.curviness) / 2;
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
      color: curvinessColour(value), weight: 5.5, opacity: 1,
    }).bindPopup(
      `<b>km ${(b.distance_m / 1000).toFixed(1)}</b><br>`
      + `${Math.round(value)}°/km — ${curvinessLabel(value)}`,
    ).addTo(layers.curviness);
  }
}

export function setCurvinessMode(enabled) {
  if (enabled) {
    map.removeLayer(layers.route);
    layers.curviness.addTo(map);
  } else {
    map.removeLayer(layers.curviness);
    layers.route.addTo(map);
  }
}

// ------------------------------------------------------------------- weather

export function drawWeather(points) {
  layers.weather.clearLayers();
  points.forEach((point) => {
    L.circleMarker([point.lat, point.lon], {
      radius: 9, color: '#14171c', weight: 2,
      fillColor: scoreFill(point.rideability), fillOpacity: 0.95,
    }).bindPopup(
      `<b>km ${(point.distance_m / 1000).toFixed(0)} — ${localTime(point.eta)}</b><br>`
      + `${esc(point.description)}<br>`
      + `${point.temperature_c ?? '?'} °C (feels ${point.apparent_c ?? '?'})<br>`
      + `Wind ${point.wind_kmh ?? '?'} km/h, gusts ${point.gust_kmh ?? '?'} km/h<br>`
      + `Rain ${point.precipitation_mm ?? 0} mm/h (${point.precipitation_probability ?? 0}%)<br>`
      + `<b>Rideability ${point.rideability}/100</b>`
      + (point.warnings.length ? `<br>${point.warnings.map(esc).join('<br>')}` : ''),
    ).addTo(layers.weather);
  });
}

function scoreFill(score) {
  if (score >= 75) return '#4ec97f';
  if (score >= 45) return '#ffc74a';
  return '#ff6b5e';
}

// ---------------------------------------------------------------------- POIs

const POI_STYLE = {
  fuel: { colour: '#4ea8ff', label: 'Fuel' },
  cafe: { colour: '#c98cff', label: 'Coffee' },
  viewpoint: { colour: '#4ec97f', label: 'Viewpoint' },
};

export function drawPois(pois, visibleCategories) {
  layers.pois.clearLayers();

  pois.forEach((poi) => {
    if (!visibleCategories.has(poi.category)) return;
    const style = POI_STYLE[poi.category] || POI_STYLE.cafe;
    // A recommended fuel stop is a decision the plan depends on, so it is drawn
    // larger and ringed rather than as one more dot among hundreds.
    const recommended = poi.category === 'fuel' && poi.recommended;

    L.circleMarker([poi.lat, poi.lon], {
      radius: recommended ? 9 : 5,
      color: recommended ? '#ffffff' : '#14171c',
      weight: recommended ? 3 : 1,
      fillColor: style.colour,
      fillOpacity: 0.95,
    }).bindPopup(
      `<b>${esc(poi.name)}</b>${recommended ? ' <em>(planned fuel stop)</em>' : ''}<br>`
      + (poi.detail ? `${esc(poi.detail)}<br>` : '')
      + `<small>km ${(poi.distance_along_route_m / 1000).toFixed(1)} · `
      + `${Math.round(poi.distance_off_route_m)} m off route</small><br>`
      + `<a href="${esc(poi.osm_url)}" target="_blank" rel="noopener">View in OpenStreetMap</a>`,
    ).addTo(layers.pois);
  });
}

// ------------------------------------------------------------------- hazards

export function drawHazards(hazards) {
  layers.hazards.clearLayers();
  hazards.forEach((hazard) => {
    L.circleMarker([hazard.lat, hazard.lon], {
      radius: 8, color: '#14171c', weight: 2,
      fillColor: hazard.severity === 'closed' ? '#ff6b5e' : '#ffc74a',
      fillOpacity: 0.95,
    }).bindPopup(
      `<b>${esc(hazard.label)}</b><br>${esc(hazard.detail)}<br>`
      + `<small>km ${(hazard.distance_along_route_m / 1000).toFixed(1)} · `
      + `${Math.round(hazard.distance_off_route_m)} m off route</small><br>`
      + `<a href="${esc(hazard.osm_url)}" target="_blank" rel="noopener">View in OpenStreetMap</a>`,
    ).addTo(layers.hazards);
  });
}

// ----------------------------------------------------------------- incidents

export function drawIncidents(incidents) {
  layers.incidents.clearLayers();
  incidents.forEach((incident) => {
    // Diamond-ish marker so live incidents read differently from OSM closures
    // even before you look at the colour.
    L.marker([incident.lat, incident.lon], {
      icon: L.divIcon({
        className: 'incident-pin',
        html: `<span class="incident-dot ${esc(incident.category)}"></span>`,
        iconSize: [16, 16],
        iconAnchor: [8, 8],
      }),
    }).bindPopup(
      `<b>${esc(incident.title)}</b><br>`
      + (incident.detail ? `${esc(incident.detail)}<br>` : '')
      + `<small>${esc(incident.source)} · km `
      + `${(incident.distance_along_route_m / 1000).toFixed(1)}</small>`
      + (incident.url ? `<br><a href="${esc(incident.url)}" target="_blank" rel="noopener">Details</a>` : ''),
    ).addTo(layers.incidents);
  });
}

// ---------------------------------------------------------------- alternates

export function drawAlternates(alternates) {
  layers.alternates.clearLayers();
  const colours = ['#4ea8ff', '#b48cff', '#4ec97f'];
  const drawn = [];

  alternates.forEach((alt, index) => {
    const line = L.polyline(alt.coordinates, {
      color: colours[index % colours.length],
      weight: 4, opacity: 0.85, dashArray: '8 6',
    }).bindPopup(
      `<b>${esc(alt.label)}</b><br>${alt.distance_km} km · ${alt.duration_min} min<br>`
      + `Corners: ${alt.curviness}°/km`,
    ).addTo(layers.alternates);
    drawn.push(line);
  });

  return { colours, lines: drawn };
}

export function focusAlternate(line) {
  map.fitBounds(line.getBounds(), { padding: [30, 30] });
  line.openPopup();
}

// ------------------------------------------------------- elevation crosshair

export function showPositionAt(distanceM) {
  const points = currentRoute ? currentRoute.lines.flat() : [];
  if (points.length < 2) return;

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

export function hidePosition() {
  positionMarker.remove();
}

function haversine(lat1, lon1, lat2, lon2) {
  const toRad = Math.PI / 180;
  const dLat = (lat2 - lat1) * toRad;
  const dLon = (lon2 - lon1) * toRad;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.sqrt(h));
}
