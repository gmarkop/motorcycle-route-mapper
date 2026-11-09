/* The mission card: what can end a ride, on half a sheet of paper.
 *
 * The idea is a fighter pilot's kneeboard card. Not the route sheet, which has
 * a map, a profile and every fuel stop, but the short list you glance at on a
 * stop: where the weather turns, where the wind is, where the road is twisty
 * and steep, and where it may be shut. Four kinds of thing, in the order you
 * meet them, and nothing else. Fuel and sunset are left off on purpose: the
 * navigation app shows both while you ride, and a card that repeats it is a
 * card with less room for what the app does not show.
 *
 * Pure, like format.js: the payloads go in, rows and markup come out. The card
 * is built from what the panels already hold, so it says what the screen said
 * when you printed it rather than asking the services again.
 */

import { esc } from './format.js';

/** Stretches of one kind closer than this are one line on the card. A run of
 *  six hairpins with a few hundred metres of straight between them is one
 *  section of road to a rider, and six lines for it would push the closures
 *  off the bottom of the card. */
export const MERGE_GAP_M = 2000;

/** The four kinds, in the order they are listed when two share a kilometre:
 *  a shut road first, because nothing after it on that km applies. */
export const KINDS = {
  road: { tag: 'ROAD', label: 'closures & roadworks', order: 0 },
  wind: { tag: 'WIND', label: 'strong gusts', order: 1 },
  wx: { tag: 'WX', label: 'bad weather', order: 2 },
  ts: { tag: 'T&S', label: 'twisty & steep', order: 3 },
};

// The weather service words every warning itself; these read the start of
// those words. tests/test_mission.py runs every warning the service can
// produce through this function, so a reworded warning fails a test instead of
// quietly dropping off the card.
const WIND = /^(Strong gusts|Gusty)\b/;
// Cold at 4-9 degrees and heat are about what to wear, not about the road:
// "heated grips weather" is not something to slow down for.
const COMFORT = /^(Cold|Heat) \(/;

/** 'wind', 'wx', or null for a warning that does not belong on the card. */
export function warningKind(text) {
  if (WIND.test(text)) return 'wind';
  if (COMFORT.test(text)) return null;
  return 'wx';
}

/** Degrees of slope from a percentage: 10% is 6 degrees, not 10. */
export function slopeDegrees(gradientPct) {
  return (Math.atan(Math.abs(gradientPct || 0) / 100) * 180) / Math.PI;
}

/**
 * When the plan puts you at a distance: the departure plus distance over
 * speed, with no stops. It is the weather service's own timing model, so a
 * closure and a shower at the same km get the same time.
 */
export function etaAt(metres, departure, speedKmh) {
  const start = new Date(departure);
  if (Number.isNaN(start.getTime()) || !(speedKmh > 0)) return null;
  return new Date(start.getTime() + (metres / 1000 / speedKmh) * 3600 * 1000).toISOString();
}

/**
 * Everything the card lists, nearest first, and what could not be checked.
 *
 * `unchecked` matters as much as `rows`. A card that says nothing about
 * weather because the forecast was never loaded looks exactly like a card
 * for a dry day, and only one of those is safe to ride on.
 */
export function missionRows({ weather, hazards, curviness, departure, speedKmh }) {
  const rows = [];
  const unchecked = [];
  const at = (metres) => etaAt(metres, departure, speedKmh);

  if (!weather) {
    unchecked.push('Weather and wind not loaded yet: press Refresh live data.');
  } else if (!weather.available) {
    unchecked.push(`Weather and wind not checked. ${weather.reason || ''}`.trim());
  } else {
    const points = weather.points || [];
    rows.push(...weatherRows(points, 'wx'), ...weatherRows(points, 'wind'));
  }

  if (!curviness) {
    unchecked.push('Twisty & steep not loaded yet.');
  } else if (!(curviness.elevation || {}).available) {
    unchecked.push('Twisty & steep not checked: '
      + `${(curviness.elevation || {}).reason || 'no heights for this route.'}`);
  } else {
    rows.push(...demandingRows(curviness.demanding || [], at));
  }

  if (!hazards) {
    unchecked.push('Closures not loaded yet.');
  } else if (!hazards.available) {
    unchecked.push(`Closures not checked. ${hazards.reason || ''}`.trim());
  } else {
    rows.push(...hazardRows(hazards.hazards || [], at));
  }

  rows.sort((a, b) => (a.from_m - b.from_m) || (KINDS[a.kind].order - KINDS[b.kind].order));
  return { rows, unchecked };
}

/**
 * Consecutive forecast points with the same kind of warning, as one row.
 *
 * Rain at km 100, 125, 150 and 175 is one wet stretch, not four rows. The
 * worst point in the run speaks for it: lowest score for weather, strongest
 * gust for wind.
 */
function weatherRows(points, kind) {
  const runs = [];
  let run = null;
  for (const point of points) {
    const hits = (point.warnings || []).filter((w) => warningKind(w) === kind);
    if (!hits.length) {
      run = null;
      continue;
    }
    if (!run) {
      run = { first: point, last: point, worst: point, hits, count: 0 };
      runs.push(run);
    }
    run.last = point;
    run.count += 1;
    const worse = kind === 'wind'
      ? (point.gust_kmh || 0) > (run.worst.gust_kmh || 0)
      : point.rideability < run.worst.rideability;
    if (worse) {
      run.worst = point;
      run.hits = hits;
    }
  }

  return runs.map((r) => {
    const arrow = kind === 'wind' && r.worst.push_deg != null
      ? `<span class="mc-arrow" style="transform: rotate(${Number(r.worst.push_deg)}deg)"`
        + ' aria-hidden="true">↑</span> '
      : '';
    const worstOf = r.count > 1 ? ` <span class="mc-small">(worst of ${r.count})</span>` : '';
    return {
      kind,
      from_m: r.first.distance_m,
      to_m: r.last.distance_m,
      eta: r.first.eta,
      html: arrow + r.hits.map(esc).join(' · ') + worstOf,
    };
  });
}

/** Groups of items whose gaps are all under MERGE_GAP_M, in route order. */
function clusters(items, start, end) {
  const groups = [];
  for (const item of [...items].sort((a, b) => start(a) - start(b))) {
    const last = groups[groups.length - 1];
    if (last && start(item) - end(last[last.length - 1]) < MERGE_GAP_M) last.push(item);
    else groups.push([item]);
  }
  return groups;
}

function demandingRows(stretches, at) {
  return clusters(stretches, (s) => s.from_m, (s) => s.to_m).map((group) => {
    const steepest = group.reduce((a, b) => (
      Math.abs(b.gradient_pct) > Math.abs(a.gradient_pct) ? b : a));
    const way = steepest.descending ? 'down' : 'up';
    const degrees = Math.round(slopeDegrees(steepest.gradient_pct));
    const from = group[0].from_m;
    const to = group[group.length - 1].to_m;
    const html = group.length === 1
      ? `${((to - from) / 1000).toFixed(1)} km, ${way} ${degrees}°`
      : `${group.length} stretches over ${((to - from) / 1000).toFixed(1)} km, `
        + `steepest ${way} ${degrees}°`;
    return { kind: 'ts', from_m: from, to_m: to, eta: at(from), html };
  });
}

/** Under this many characters a hazard's detail goes on the card; past it, the
 *  card would wrap a road name over three lines. */
const DETAIL_CHARS = 48;

function hazardRows(hazards, at) {
  // Merged only with their own kind: a lift gate next to a closed road are two
  // facts, but one road mapped as six construction segments is one.
  const byLabel = new Map();
  for (const hazard of hazards) {
    if (!byLabel.has(hazard.label)) byLabel.set(hazard.label, []);
    byLabel.get(hazard.label).push(hazard);
  }

  const rows = [];
  for (const [label, same] of byLabel) {
    const along = (h) => h.distance_along_route_m;
    for (const group of clusters(same, along, along)) {
      const first = group[0];
      const detail = group.length === 1 && first.detail && first.detail.length <= DETAIL_CHARS
        ? ` <span class="mc-small">${esc(first.detail)}</span>` : '';
      const many = group.length > 1 ? ` ×${group.length}` : '';
      const text = first.severity === 'closed' ? `<strong>${esc(label)}</strong>` : esc(label);
      rows.push({
        kind: 'road',
        from_m: along(first),
        to_m: along(group[group.length - 1]),
        eta: at(along(first)),
        html: text + many + detail,
      });
    }
  }
  return rows;
}

// ------------------------------------------------------------- silhouettes

/*
 * Top-view planforms, drawn for this card: the right half of each aircraft as
 * a list of points, nose at the top of a 70 x 100 box, mirrored about x = 35.
 * Drawing half and mirroring keeps the two sides from disagreeing, which a
 * hand-drawn path always does by a pixel somewhere.
 */
const CENTRE = 35;

// F-16: blended wing root, cropped delta wings swept about 40 degrees,
// missile rails on the tips, all-moving tailplanes, one nozzle.
const F16_HALF = [
  [35, 0], [37.2, 10], [38.4, 22], [38.8, 32], [42, 46], [63.5, 63],
  [63.5, 70], [43, 70], [40, 72.5], [41, 79], [54, 88], [54, 91.5],
  [41.5, 90.5], [38.6, 93], [37.6, 100], [35, 100],
];
const F16_RAIL = [[63.5, 52], [65.5, 52], [65.5, 75], [63.5, 75]];

// F-5: a long slim nose, small trapezoid wings set well aft, tip missiles,
// and two nozzles side by side -- the notch between them is the giveaway.
const F5_HALF = [
  [35, 0], [36.1, 12], [37.3, 25], [38, 34], [38.6, 42], [40.5, 51],
  [58, 62.5], [58, 67], [40.5, 69.5], [39.8, 76], [40.4, 81], [50.5, 88],
  [50.5, 91], [40.2, 90.5], [39, 94], [39, 100], [35.8, 100], [35, 97.5],
];
const F5_RAIL = [[58, 55], [59.6, 55], [59.6, 73], [58, 73]];

/** An SVG path for a shape given as its right half, mirrored to the left. */
export function mirroredPath(half) {
  const left = [...half].reverse().map(([x, y]) => [2 * CENTRE - x, y]);
  const all = [...half, ...left];
  return `M${all.map(([x, y]) => `${+x.toFixed(2)},${y}`).join('L')}Z`;
}

function railPaths(rail) {
  const mirrored = rail.map(([x, y]) => [2 * CENTRE - x, y]);
  return [rail, mirrored]
    .map((pts) => `M${pts.map(([x, y]) => `${+x.toFixed(2)},${y}`).join('L')}Z`)
    .join('');
}

function silhouette(name, half, rail, canopy) {
  return `<svg class="mc-jet" viewBox="0 0 70 100" role="img" aria-label="${name} silhouette">`
    + `<path d="${mirroredPath(half)}${railPaths(rail)}" fill="#000"/>`
    + `<ellipse cx="35" cy="${canopy[0]}" rx="1.3" ry="${canopy[1]}" fill="#fff"/></svg>`;
}

export const F16_SVG = silhouette('F-16', F16_HALF, F16_RAIL, [17, 5.5]);
export const F5_SVG = silhouette('F-5', F5_HALF, F5_RAIL, [27, 4.5]);

// -------------------------------------------------------------------- card

/**
 * A 24-hour clock in the browser's zone, whatever its locale. The rest of the
 * page follows the locale, which in Greek is "6:23 μ.μ." -- three characters
 * wider than 18:23 in a column that has to stay narrow, and slower to read.
 */
export function clock(iso) {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
}

function kmCell(row) {
  const from = Math.round(row.from_m / 1000);
  const to = Math.round(row.to_m / 1000);
  return to > from ? `${from}–${to}` : `${from}`;
}

/** One card's markup. The caller prints it twice. */
export function missionCard({
  name, departure, speedKmh, distance, rows, unchecked, checkedAt, printedAt,
}) {
  const start = new Date(departure);
  const day = Number.isNaN(start.getTime()) ? ''
    : start.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
  const facts = [day && `${day} · dep ${clock(departure)}`, distance,
                 speedKmh && `${speedKmh} km/h`].filter(Boolean).join(' · ');

  const body = rows.length
    ? rows.map((row) => `<tr class="mc-${row.kind}" data-km="${Math.round(row.from_m / 1000)}">`
        + `<td class="mc-km">${kmCell(row)}</td>`
        + `<td class="mc-time">${esc(clock(row.eta))}</td>`
        + `<td class="mc-what"><b class="mc-tag">${esc(KINDS[row.kind].tag)}</b> ${row.html}</td>`
        + '</tr>').join('')
    : `<tr><td colspan="3" class="mc-none">${unchecked.length
        ? 'Nothing found in what was checked.'
        : 'Nothing to report: no bad weather, strong gusts, twisty &amp; steep '
          + 'stretches or closures on this route.'}</td></tr>`;

  const legend = Object.values(KINDS).map((k) => `<b>${esc(k.tag)}</b> ${esc(k.label)}`).join(' · ');
  const footer = [
    checkedAt && `Forecast checked ${esc(checkedAt)}.`,
    speedKmh && `Times at ${speedKmh} km/h with no stops.`,
    printedAt && `Printed ${esc(printedAt)}.`,
  ].filter(Boolean).join(' ');

  // Divs, not <header>/<footer>: the route sheet's print stylesheet hides
  // every <header> on the page, and took the silhouettes and title with it.
  return `<div class="mc-head">${F16_SVG}
      <div class="mc-title"><h1>${esc(name)}</h1><div class="mc-facts">${esc(facts)}</div></div>
      ${F5_SVG}</div>
    ${unchecked.map((u) => `<p class="mc-unchecked">${esc(u)}</p>`).join('')}
    <div class="mc-list"><table>
      <thead><tr><th class="mc-km">KM</th><th class="mc-time">TIME</th><th></th></tr></thead>
      <tbody>${body}</tbody></table></div>
    <div class="mc-foot"><div>${legend}</div><div>${footer}</div></div>`;
}
