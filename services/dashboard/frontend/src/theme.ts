// Shared visual constants — matches the dashboard's existing dark palette (services/dashboard/app.py styles)
// so the React grid feels native to the rest of the dashboard.

export const COLORS = {
  bg: "#0f1115",
  panel: "#171a21",
  panelAlt: "#13161c",
  border: "#262b35",
  text: "#d7dce2",
  muted: "#8b949e",
  link: "#58a6ff",
  // Coverage darkness ramp (untrusted/default): blue-grey, light = low coverage, dark/bright = high. We paint
  // on the dark bg, so higher coverage = a BRIGHTER cell (more visible), absent = bg.
  coverageHue: "#6ea8ff",
  // Trust overlay: trusted cells render green-tinted, untrusted stay neutral grey. Binary only.
  trusted: "#3fb950",
  untrusted: "#7d8590",
} as const;

// Canvas cell sizing (CSS px before devicePixelRatio scaling). Small boxes = the HEIC tiny-boxes aesthetic.
export const CELL = {
  w: 7,
  h: 7,
  gap: 0,
} as const;
