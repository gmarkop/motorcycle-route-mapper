/* Service worker: keeps the app usable when the signal is not.
 *
 * Two caches with different rules, because the two kinds of content fail
 * differently:
 *
 *   shell  - the HTML, CSS, JS and Leaflet. Small, changes only when you update
 *            the app. Cache-first, refreshed in the background.
 *   tiles  - map imagery. Large, immutable per coordinate, and the thing you
 *            actually lose in a valley. Cache-first, never revalidated.
 *
 * API responses are deliberately NOT cached here. They are live data with their
 * own TTL cache on the server, and a stale forecast served silently by a
 * service worker is worse than an honest "could not refresh".
 */

const VERSION = 'v1';
const SHELL_CACHE = `moto-shell-${VERSION}`;
const TILE_CACHE = `moto-tiles-${VERSION}`;

const SHELL_ASSETS = [
  '/',
  '/static/style.css',
  '/static/js/app.js',
  '/static/js/format.js',
  '/static/js/mapview.js',
  '/static/js/panels.js',
  '/static/js/tiles.js',
  '/static/vendor/leaflet/leaflet.js',
  '/static/vendor/leaflet/leaflet.css',
];

/** Hosts whose responses are map tiles. Kept explicit: caching every
 *  cross-origin image would fill the disk with things that are not maps. */
const TILE_HOSTS = ['tile.openstreetmap.org', 'tile.opentopomap.org'];

//: Hard ceiling on stored tiles, independent of what the page asks for.
const MAX_TILES = 3000;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      // addAll is atomic: one 404 would reject the whole install and leave the
      // app with no worker at all, so failures are tolerated per asset.
      .then((cache) => Promise.allSettled(SHELL_ASSETS.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => name.startsWith('moto-') && name !== SHELL_CACHE && name !== TILE_CACHE)
          .map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

function isTileRequest(url) {
  return TILE_HOSTS.some((host) => url.hostname.endsWith(host));
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  if (isTileRequest(url)) {
    event.respondWith(cacheFirst(request, TILE_CACHE));
    return;
  }

  if (url.origin === self.location.origin) {
    // Live data must never be served from a cache without the user knowing.
    if (url.pathname.startsWith('/api/')) return;
    event.respondWith(staleWhileRevalidate(request, SHELL_CACHE));
  }
});

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;

  try {
    const response = await fetch(request);
    // Opaque cross-origin responses still cache and replay fine for images.
    if (response && (response.ok || response.type === 'opaque')) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    // Offline with nothing cached: a transparent tile beats a broken image icon.
    return blankTile();
  }
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);

  const network = fetch(request)
    .then((response) => {
      if (response && response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => hit);

  return hit || network;
}

/** A 1x1 fully transparent PNG (RGBA 0,0,0,0), so an uncached tile shows the
 *  map background rather than a broken-image icon. Check the alpha channel if
 *  you ever swap this constant: plenty of "blank pixel" snippets in the wild
 *  are actually a coloured pixel, which tiles the whole viewport in that colour. */
function blankTile() {
  const pixel = Uint8Array.from(atob(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=',
  ), (c) => c.charCodeAt(0));
  return new Response(pixel, { headers: { 'Content-Type': 'image/png' } });
}

// ---------------------------------------------------------------- page control

self.addEventListener('message', (event) => {
  const { type } = event.data || {};
  if (type === 'PREFETCH_TILES') {
    event.waitUntil(prefetchTiles(event.data.urls || [], event.data.delayMs || 120));
  } else if (type === 'CLEAR_TILES') {
    event.waitUntil(caches.delete(TILE_CACHE).then(() => report({ type: 'TILES_CLEARED' })));
  } else if (type === 'TILE_STATS') {
    event.waitUntil(tileStats());
  }
});

async function prefetchTiles(urls, delayMs) {
  const cache = await caches.open(TILE_CACHE);
  let stored = 0;
  let failed = 0;

  for (let i = 0; i < urls.length && stored < MAX_TILES; i += 1) {
    const url = urls[i];
    try {
      if (await cache.match(url)) {
        stored += 1;                       // already held; nothing to fetch
      } else {
        const response = await fetch(url, { mode: 'no-cors' });
        if (response && (response.ok || response.type === 'opaque')) {
          await cache.put(url, response.clone());
          stored += 1;
        } else {
          failed += 1;
        }
        // Deliberate throttle. These are somebody else's free tile servers and
        // a burst of a few hundred requests is exactly what gets clients banned.
        await sleep(delayMs);
      }
    } catch (err) {
      failed += 1;
    }

    if (i % 10 === 0 || i === urls.length - 1) {
      report({ type: 'PREFETCH_PROGRESS', done: i + 1, total: urls.length, stored, failed });
    }
  }

  report({ type: 'PREFETCH_DONE', stored, failed, total: urls.length });
}

async function tileStats() {
  const cache = await caches.open(TILE_CACHE);
  const keys = await cache.keys();
  report({ type: 'TILE_STATS', count: keys.length });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function report(message) {
  const clients = await self.clients.matchAll({ includeUncontrolled: true });
  clients.forEach((client) => client.postMessage(message));
}
