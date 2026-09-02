/* Pure formatting helpers: no DOM, no map, no state — so they are the one part
 * of the frontend that can be reasoned about in isolation. */

/** Escape text before it goes into innerHTML. Route files and OSM tags are both
 *  user-supplied, and a cafe legitimately named `Bar <2>` must not become markup. */
export function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export const km = (metres) => `${(metres / 1000).toFixed(1)} km`;
export const kmRounded = (metres) => `${Math.round(metres / 1000)}`;

export function localTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function scoreClass(score) {
  if (score >= 75) return 'good';
  if (score >= 45) return 'mid';
  return 'bad';
}

export function scoreColour(score) {
  if (score >= 75) return '#4ec97f';
  if (score >= 45) return '#ffc74a';
  return '#ff6b5e';
}

/* Curviness scale, in degrees of heading change per kilometre.
 *
 * The anchors come from what the numbers mean on real roads rather than from a
 * even split of the range: a motorway sits near zero, a decent country road
 * lands around 60-120, and only genuine mountain switchbacks pass 250. A linear
 * ramp to the maximum would paint every ordinary back road the same dull colour.
 */
const CURVINESS_STOPS = [
  { at: 0,   colour: [110, 123, 145] },  // grey — straight
  { at: 40,  colour: [78,  168, 255] },  // blue — gentle
  { at: 100, colour: [78,  201, 127] },  // green — flowing
  { at: 180, colour: [255, 199, 74]  },  // amber — properly twisty
  { at: 280, colour: [255, 107, 94]  },  // red — switchbacks
];

export function curvinessColour(value) {
  const stops = CURVINESS_STOPS;
  if (value <= stops[0].at) return rgb(stops[0].colour);
  for (let i = 1; i < stops.length; i += 1) {
    if (value <= stops[i].at) {
      const span = stops[i].at - stops[i - 1].at;
      const t = span === 0 ? 0 : (value - stops[i - 1].at) / span;
      return rgb(mix(stops[i - 1].colour, stops[i].colour, t));
    }
  }
  return rgb(stops[stops.length - 1].colour);
}

/** CSS gradient matching curvinessColour, for the map legend. */
export function curvinessGradient() {
  const max = CURVINESS_STOPS[CURVINESS_STOPS.length - 1].at;
  const parts = CURVINESS_STOPS.map(
    (stop) => `${rgb(stop.colour)} ${Math.round((stop.at / max) * 100)}%`,
  );
  return `linear-gradient(to right, ${parts.join(', ')})`;
}

export function curvinessLabel(value) {
  if (value < 25) return 'straight';
  if (value < 70) return 'gentle';
  if (value < 130) return 'flowing';
  if (value < 220) return 'twisty';
  return 'switchbacks';
}

function mix(a, b, t) {
  return [0, 1, 2].map((i) => Math.round(a[i] + (b[i] - a[i]) * t));
}

function rgb([r, g, b]) {
  return `rgb(${r}, ${g}, ${b})`;
}
