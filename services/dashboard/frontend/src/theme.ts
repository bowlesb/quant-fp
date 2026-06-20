// Visual system for the coverage grid — a refined dark palette + perceptual coverage/trust ramps. Kept in one
// place so the canvas, the legend swatches, and the panels stay in lockstep.

export const COLORS = {
  bg: "#0d1017",
  bgGrid: "#0a0c12", // the heatmap well, a touch darker than the chrome so the cells pop
  panel: "#161b24",
  panelAlt: "#11151d",
  border: "#232a36",
  borderSoft: "#1b212b",
  text: "#e6edf3",
  textDim: "#aeb9c6",
  muted: "#7d8896",
  link: "#6cb6ff",
  trusted: "#3fb950",
  untrusted: "#8b98a6",
  accent: "#6cb6ff",
} as const;

// Canvas cell sizing (CSS px before devicePixelRatio scaling). Small boxes = the HEIC tiny-boxes aesthetic.
export const CELL = {
  w: 8,
  h: 8,
  gap: 1, // a hairline gap between cells reads as crisp tiles rather than a smear
} as const;

type Rgb = [number, number, number];

function lerp(a: number, b: number, t: number): number {
  return Math.round(a + (b - a) * t);
}

function lerpRamp(stops: Rgb[], t: number): Rgb {
  const clamped = Math.max(0, Math.min(1, t));
  if (clamped <= 0) return stops[0];
  if (clamped >= 1) return stops[stops.length - 1];
  const scaled = clamped * (stops.length - 1);
  const i = Math.floor(scaled);
  const frac = scaled - i;
  const lo = stops[i];
  const hi = stops[i + 1];
  return [lerp(lo[0], hi[0], frac), lerp(lo[1], hi[1], frac), lerp(lo[2], hi[2], frac)];
}

// Coverage ramp (default / trust-overlay OFF): a calm deep-navy → blue → cyan → near-white sweep. Perceptually
// increasing brightness with coverage, so a denser cell reads brighter against the dark well.
const COVERAGE_STOPS: Rgb[] = [
  [22, 38, 66], // faint
  [38, 86, 158],
  [56, 140, 222],
  [120, 200, 255],
  [214, 240, 255], // full
];

// Trusted ramp (trust-overlay ON, cell all-trusted): the same brightness sweep in green.
const TRUSTED_STOPS: Rgb[] = [
  [20, 52, 30],
  [33, 100, 52],
  [54, 150, 78],
  [104, 200, 120],
  [190, 240, 200],
];

// Untrusted ramp (trust-overlay ON, cell has any untrusted group): a neutral slate sweep — present but
// visibly NOT green, so trusted vs untrusted reads at a glance without a hard binary cliff in darkness.
const UNTRUSTED_STOPS: Rgb[] = [
  [40, 46, 55],
  [70, 78, 90],
  [104, 114, 128],
  [150, 160, 173],
  [205, 212, 221],
];

function rgbCss(rgb: Rgb): string {
  return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
}

// The fill a cell paints, given its coverage byte (0..255), trust bit, and whether the trust overlay is on.
// Byte 0 (absent) -> null = paint nothing (the well shows through). We gamma-lift the low end slightly so even
// thin coverage is legible.
export function cellColor(coverageByte: number, trustedBit: number, overlay: boolean): string | null {
  if (coverageByte <= 0) return null;
  const t = Math.pow(coverageByte / 255, 0.85);
  if (!overlay) return rgbCss(lerpRamp(COVERAGE_STOPS, t));
  return rgbCss(lerpRamp(trustedBit ? TRUSTED_STOPS : UNTRUSTED_STOPS, t));
}

// Sample CSS colours for the legend swatches (low + high end of each ramp).
export const LEGEND = {
  coverageLow: rgbCss(COVERAGE_STOPS[1]),
  coverageHigh: rgbCss(COVERAGE_STOPS[COVERAGE_STOPS.length - 1]),
  trustedHigh: rgbCss(TRUSTED_STOPS[TRUSTED_STOPS.length - 1]),
  untrustedHigh: rgbCss(UNTRUSTED_STOPS[UNTRUSTED_STOPS.length - 1]),
} as const;
