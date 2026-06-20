import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA IS the dashboard: served by the dashboard FastAPI app at the ROOT "/" (StaticFiles mount), so asset
// URLs are root-absolute. The dev server proxies /api to the live dashboard on :8088 so `npm run dev` works
// against the real always-warm worker data without a separate backend run.
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    // Emitted into ./dist; the Dockerfile node stage builds this and copies dist into the image at
    // /app/frontend/store-grid, which app.py mounts as StaticFiles at "/". Source maps off for a lean image.
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8088",
        changeOrigin: true,
      },
    },
  },
});
