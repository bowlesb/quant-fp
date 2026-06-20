import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served by the dashboard FastAPI app under /store-grid (StaticFiles mount), so all asset URLs
// must be prefixed with that base. The dev server proxies /api to the live dashboard on :8088 so `npm run
// dev` works against the real always-warm worker data without a separate backend run.
export default defineConfig({
  base: "/store-grid/",
  plugins: [react()],
  build: {
    // Emitted into ./dist; the Dockerfile node stage builds this and copies dist into the image at
    // /app/frontend/store-grid, which app.py mounts as StaticFiles. Source maps off for a lean image.
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
