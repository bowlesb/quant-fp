// Visual system for the coverage grid (v3 — light theme). WHITE = zero coverage = the page background, so an
// absent cell and a zero-coverage cell look identical. Coverage DARKENS each cell toward its column's colour:
// a trusted feature-group → dark BLUE, an untrusted group → dark RED, a raw tape layer → dark SLATE. So the
// darker a cell, the more of the full universe that column covers that date; the colour tells you trust at a
// glance. There is no trust toggle — trust is always shown via blue-vs-red.

export const COLORS = {
  bg: "#ffffff", // the page / grid background == a zero-coverage cell
  panel: "#f6f8fa",
  panelAlt: "#eef1f5",
  border: "#d6dbe1",
  borderSoft: "#e7ebf0",
  text: "#1b2430",
  textDim: "#4a5563",
  muted: "#7a8694",
  trustedDark: "#0b3d91", // trusted group, fully covered
  untrustedDark: "#a01722", // untrusted group, fully covered
  rawDark: "#3a4554", // raw tape layer, fully covered (neutral slate)
  accent: "#1f6feb",
} as const;

// Canvas cell sizing (CSS px before devicePixelRatio scaling). ~66 columns fit one screen; the date axis
// (hundreds of rows) scrolls vertically. A hairline gap reads as crisp tiles.
export const CELL = {
  w: 19,
  h: 9,
  gap: 1,
} as const;

export type ColumnKind = "raw" | "group";

type Rgb = [number, number, number];

const WHITE: Rgb = [255, 255, 255];

function hexToRgb(hex: string): Rgb {
  const value = parseInt(hex.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

const TRUSTED_RGB = hexToRgb(COLORS.trustedDark);
const UNTRUSTED_RGB = hexToRgb(COLORS.untrustedDark);
const RAW_RGB = hexToRgb(COLORS.rawDark);

function mix(a: Rgb, b: Rgb, t: number): string {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r},${g},${bl})`;
}

// The dark end for a column: trusted → blue, untrusted → red, raw → slate.
export function columnDark(kind: ColumnKind, trusted: boolean): Rgb {
  if (kind === "raw") return RAW_RGB;
  return trusted ? TRUSTED_RGB : UNTRUSTED_RGB;
}

// The fill a cell paints: WHITE at zero coverage → the column's dark colour at full. Returns null at byte 0 so
// an absent cell paints nothing (the white background shows through — identical to zero coverage). A gentle
// gamma lift keeps thin coverage visible against white.
export function cellColor(coverageByte: number, kind: ColumnKind, trusted: boolean): string | null {
  if (coverageByte <= 0) return null;
  const t = Math.pow(coverageByte / 255, 0.7);
  return mix(WHITE, columnDark(kind, trusted), t);
}

// Sample CSS for the legend swatches (full-coverage dark end of each kind).
export const LEGEND = {
  trustedDark: `rgb(${TRUSTED_RGB.join(",")})`,
  untrustedDark: `rgb(${UNTRUSTED_RGB.join(",")})`,
  rawDark: `rgb(${RAW_RGB.join(",")})`,
} as const;
