/* Client-side persistence for loaded rides.
 *
 * The problem this solves: the parsed route lives only in the server's memory
 * (`RouteStore` in api.py). The service worker caches the app shell and the map
 * tiles, so offline the interface loads — but with no route in it. Reload the
 * tab in an Alpine valley and you get an empty app, which is the one moment you
 * needed it.
 *
 * So a ride is kept here: the parsed route, the last good answer from each live
 * layer, and — importantly — the original file's bytes. Keeping the file is
 * what makes recovery transparent: the server's route ids are ephemeral, so
 * after a server restart every id 404s. With the bytes on hand the app can
 * re-upload silently and carry on, instead of asking the rider to find the GPX
 * again on a phone in the rain.
 *
 * IndexedDB rather than localStorage because it stores Blobs natively and is
 * not capped at a few megabytes; a dense GPX is easily larger than that.
 */

const DB_NAME = 'moto-route';
const DB_VERSION = 1;
const STORE = 'rides';

/** Keep a tour's worth, not a lifetime's. Each ride holds a file blob and a
 *  parsed route, so an unbounded store would quietly eat an iPad's quota. */
const MAX_RIDES = 5;

let dbPromise = null;

/** Whether persistence is usable at all. Private browsing and locked-down
 *  configurations can leave `indexedDB` present but unusable, so callers treat
 *  every operation as best-effort and the app works without it. */
export function supported() {
  return typeof indexedDB !== 'undefined';
}

function openDb() {
  if (dbPromise) return dbPromise;

  dbPromise = new Promise((resolve, reject) => {
    if (!supported()) {
      reject(new Error('IndexedDB unavailable'));
      return;
    }
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const store = db.createObjectStore(STORE, { keyPath: 'key' });
        // Ordering by last opened is what "restore the ride I was looking at"
        // needs; savedAt would resurrect the wrong one after switching rides.
        store.createIndex('openedAt', 'openedAt');
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error('IndexedDB blocked by another tab'));
  }).catch((err) => {
    // Reset so a later call can retry rather than being stuck with a rejection.
    dbPromise = null;
    throw err;
  });

  return dbPromise;
}

function runTransaction(mode, work) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const store = tx.objectStore(STORE);
    let result;
    try {
      result = work(store);
    } catch (err) {
      reject(err);
      return;
    }
    tx.oncomplete = () => resolve(isBox(result) ? result.value : result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error('transaction aborted'));
  }));
}

/** Wrap an IDBRequest so its result is readable after the transaction commits.
 *
 *  The box is tagged rather than sniffed for a defined `value`: a `get` for a
 *  key that does not exist legitimately yields `undefined`, and unwrapping by
 *  "does it have a value" would hand that caller the box itself — a truthy
 *  object standing in for a missing ride. */
const BOX = Symbol('idb-result');

function collect(request) {
  const box = { [BOX]: true, value: undefined };
  request.onsuccess = () => { box.value = request.result; };
  return box;
}

function isBox(value) {
  return Boolean(value) && typeof value === 'object' && value[BOX] === true;
}

export function newKey() {
  // A stable client-side identity for the ride. The server's route id changes
  // whenever the file is re-uploaded, so it cannot be the primary key.
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `ride-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Persist a freshly loaded ride. `file` is kept whole so the app can re-upload
 * it without the rider going hunting for the original.
 */
export async function saveRide({ key, serverRouteId, file, route, plan }) {
  const now = Date.now();
  const record = {
    key: key || newKey(),
    serverRouteId,
    name: route && route.name ? route.name : (file ? file.name : 'Route'),
    filename: file ? file.name : '',
    distance_m: route && route.stats ? route.stats.distance_m : 0,
    file: file || null,
    route,
    plan: plan || null,
    layers: {},
    savedAt: now,
    openedAt: now,
    refreshedAt: null,
  };

  await write(record);
  await evictOldest();
  return record.key;
}

async function write(record) {
  try {
    await runTransaction('readwrite', (store) => store.put(record));
  } catch (err) {
    // A full quota is the realistic failure on a tablet. Drop the oldest ride
    // and try once more before giving up on persistence for this one.
    if (err && (err.name === 'QuotaExceededError' || err.code === 22)) {
      await evictOldest(1);
      await runTransaction('readwrite', (store) => store.put(record));
      return;
    }
    throw err;
  }
}

/**
 * Read-modify-write a ride inside a single transaction.
 *
 * This has to be atomic. Five live layers save concurrently, and doing the read
 * and the write as two separate transactions loses updates: each one reads the
 * ride before the others have written, so the last writer wins and most layers
 * silently vanish. IndexedDB serialises transactions over the same store, so
 * issuing the `put` from inside the `get` callback — same transaction — makes
 * each update see the previous one.
 */
function mutate(key, mutator) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    const request = store.get(key);
    let updated = null;

    request.onsuccess = () => {
      const existing = request.result;
      if (!existing) return;
      updated = mutator(existing) || existing;
      store.put(updated);
    };

    tx.oncomplete = () => resolve(updated);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error('transaction aborted'));
  }));
}

/** Merge fields into an existing ride. Returns the updated record, or null. */
export function patchRide(key, patch) {
  return mutate(key, (ride) => {
    const updated = { ...ride, ...patch };
    if (patch.layers) updated.layers = { ...(ride.layers || {}), ...patch.layers };
    return updated;
  });
}

/** Record one live-layer response against a ride, with the time it was live. */
export function saveLayer(key, name, payload) {
  if (!key || !payload) return Promise.resolve(null);
  return mutate(key, (ride) => {
    ride.layers = { ...(ride.layers || {}), [name]: payload };
    ride.refreshedAt = Date.now();
    return ride;
  });
}

export function getRide(key) {
  return runTransaction('readonly', (store) => collect(store.get(key)));
}

export function touchRide(key) {
  return mutate(key, (ride) => {
    ride.openedAt = Date.now();
    return ride;
  });
}

/** All rides, newest-opened first. Blobs come along; callers wanting only the
 *  list should use `listSummaries`. */
export function allRides() {
  return runTransaction('readonly', (store) => collect(store.getAll()))
    .then((rides) => (rides || []).sort((a, b) => (b.openedAt || 0) - (a.openedAt || 0)));
}

/** Lightweight rows for the saved-rides picker — no file blobs, no geometry. */
export async function listSummaries() {
  const rides = await allRides();
  return rides.map((ride) => ({
    key: ride.key,
    name: ride.name,
    filename: ride.filename,
    distance_m: ride.distance_m,
    openedAt: ride.openedAt,
    refreshedAt: ride.refreshedAt,
    layerCount: Object.keys(ride.layers || {}).length,
  }));
}

export async function mostRecent() {
  const rides = await allRides();
  return rides.length ? rides[0] : null;
}

export function deleteRide(key) {
  return runTransaction('readwrite', (store) => store.delete(key));
}

export function clearAll() {
  return runTransaction('readwrite', (store) => store.clear());
}

async function evictOldest(extra = 0) {
  const rides = await allRides();
  const keep = Math.max(0, MAX_RIDES - extra);
  const doomed = rides.slice(keep);
  for (const ride of doomed) {
    await runTransaction('readwrite', (store) => store.delete(ride.key));
  }
  return doomed.length;
}

/** Rough bytes in use, when the browser will say. Used only for display. */
export async function estimateUsage() {
  if (navigator.storage && navigator.storage.estimate) {
    try {
      const { usage } = await navigator.storage.estimate();
      return usage || 0;
    } catch {
      return 0;
    }
  }
  return 0;
}
