/* Offline map tiles.
 *
 * The touring case this exists for: you plan at the hotel on wifi, then ride
 * into a valley with no signal and the map goes blank. Caching the tiles along
 * the route beforehand fixes that.
 *
 * It is bounded on purpose. OpenStreetMap's tile usage policy forbids bulk
 * downloading and names 250 tiles as the limit for an area, so the default cap
 * matches that and requests are spaced out rather than fired in a burst. Tiles
 * are chosen along the route corridor rather than over its bounding box, which
 * is what makes 250 tiles enough to be useful: a bounding box around an alpine
 * loop is mostly mountains you will never see.
 *
 * Zoom levels are filled from the outside in — a complete low zoom first, then
 * the next one only if it fits in the budget. Partial coverage at high zoom
 * would leave holes exactly where you stopped to look.
 */

const ZOOM_LEVELS = [9, 10, 11, 12, 13];

let registration = null;
const listeners = new Set();

export function supported() {
  // Service workers need a secure context: https, or localhost. Reaching the
  // app from a phone over http://192.168.x.x will not register one.
  return 'serviceWorker' in navigator && window.isSecureContext;
}

export async function register() {
  if (!supported()) return null;
  try {
    registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    navigator.serviceWorker.addEventListener('message', (event) => {
      listeners.forEach((fn) => fn(event.data || {}));
    });
    await navigator.serviceWorker.ready;
    // `ready` means a worker is active, not that it controls this page. On the
    // first load after registering — which is every load after clearing site
    // data — `controller` is still null for a moment while the worker claims
    // its clients, and every postMessage in this module is silently dropped
    // when it is. The tile panel then sits on its initial "Checking…" for
    // ever, because the stats reply it is waiting for was never asked for.
    await untilControlling();
    return registration;
  } catch (err) {
    // Registration fails for reasons worth seeing: a scope the worker's path
    // cannot claim, an insecure origin, or the file failing to parse.
    lastError = err && err.message ? err.message : String(err);
    return null;
  }
}

let lastError = '';

export function registrationError() {
  return lastError;
}

export function onMessage(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function controller() {
  return navigator.serviceWorker && navigator.serviceWorker.controller;
}

/**
 * Resolve once a worker is controlling this page, or give up.
 *
 * The worker calls `clients.claim()`, so control arrives shortly after
 * activation and `controllerchange` is how it announces itself. The timeout is
 * not a formality: if control never arrives, the caller must be told so it can
 * say why rather than leaving a label mid-sentence.
 */
function untilControlling(timeoutMs = 5000) {
  if (controller()) return Promise.resolve(true);

  return new Promise((resolve) => {
    const done = (value) => {
      navigator.serviceWorker.removeEventListener('controllerchange', onChange);
      clearTimeout(timer);
      resolve(value);
    };
    const onChange = () => done(true);
    const timer = setTimeout(() => done(false), timeoutMs);
    navigator.serviceWorker.addEventListener('controllerchange', onChange);
  });
}

/** Whether messages to the worker will actually reach it. */
export function controlling() {
  return Boolean(controller());
}

export function requestStats() {
  if (controller()) controller().postMessage({ type: 'TILE_STATS' });
}

export function clearTiles() {
  if (controller()) controller().postMessage({ type: 'CLEAR_TILES' });
}

export function prefetch(urls, delayMs) {
  if (!controller()) return false;
  controller().postMessage({ type: 'PREFETCH_TILES', urls, delayMs });
  return true;
}

// ------------------------------------------------------------- slippy map maths

/** Clamp a tile index into the grid that exists at this zoom.
 *
 *  Both projections can land a hair outside [0, 2^z) through floating point
 *  alone: at the Mercator limit the latitude term computes as a touch over 1,
 *  and `Math.floor` of the resulting -1e-16 is -1, not 0. A negative index is a
 *  request for a tile that cannot exist.
 */
function clampTile(value, zoom) {
  return Math.max(0, Math.min(2 ** zoom - 1, value));
}

export function lonToTileX(lon, zoom) {
  return clampTile(Math.floor(((lon + 180) / 360) * 2 ** zoom), zoom);
}

export function latToTileY(lat, zoom) {
  // Web Mercator is undefined at the poles, so clamp before the tangent blows up.
  const clamped = Math.max(-85.05112878, Math.min(85.05112878, lat));
  const rad = (clamped * Math.PI) / 180;
  return clampTile(Math.floor(
    ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * 2 ** zoom,
  ), zoom);
}

/**
 * Tiles covering the route, cheapest zoom levels first, stopping before the cap.
 *
 * Returns { tiles, zooms, skipped, partial } so the UI can say what you will
 * actually have offline instead of implying it cached everything. `partial` is
 * set when even the coarsest level had to be truncated to fit the cap.
 */
export function tilesForRoute(latlngs, maxTiles, zoomLevels = ZOOM_LEVELS) {
  const tiles = [];
  const zooms = [];
  const skipped = [];
  const seen = new Set();

  for (const zoom of zoomLevels) {
    const atThisZoom = new Map();
    // A one-tile skirt at low zoom costs almost nothing and stops the map
    // ending at the edge of the road when you pan.
    const buffer = zoom <= 11 ? 1 : 0;

    for (const [lat, lon] of latlngs) {
      const cx = lonToTileX(lon, zoom);
      const cy = latToTileY(lat, zoom);
      for (let dx = -buffer; dx <= buffer; dx += 1) {
        for (let dy = -buffer; dy <= buffer; dy += 1) {
          const key = `${zoom}/${cx + dx}/${cy + dy}`;
          if (!seen.has(key)) atThisZoom.set(key, { z: zoom, x: cx + dx, y: cy + dy });
        }
      }
    }

    if (tiles.length + atThisZoom.size > maxTiles) {
      skipped.push(zoom);
      continue;
    }
    atThisZoom.forEach((tile, key) => {
      seen.add(key);
      tiles.push(tile);
    });
    zooms.push(zoom);
  }

  // A cap too small for even the coarsest level would otherwise cache nothing
  // at all, silently. Half a loaf — the overview tiles — beats none.
  let partial = false;
  if (!tiles.length && zoomLevels.length) {
    const coarsest = zoomLevels[0];
    const fallback = new Map();
    for (const [lat, lon] of latlngs) {
      const key = `${coarsest}/${lonToTileX(lon, coarsest)}/${latToTileY(lat, coarsest)}`;
      if (!fallback.has(key)) {
        fallback.set(key, { z: coarsest, x: lonToTileX(lon, coarsest), y: latToTileY(lat, coarsest) });
      }
      if (fallback.size >= maxTiles) break;
    }
    fallback.forEach((tile) => tiles.push(tile));
    if (tiles.length) {
      zooms.push(coarsest);
      partial = true;
    }
  }

  return { tiles, zooms, skipped, partial };
}

/** Fill a Leaflet URL template. `{s}` is pinned so the same tile is not fetched
 *  three times under three subdomain names, each a separate cache entry. */
export function tileUrl(template, tile) {
  return template
    .replace('{s}', 'a')
    .replace('{z}', tile.z)
    .replace('{x}', tile.x)
    .replace('{y}', tile.y)
    .replace('{r}', '');
}
