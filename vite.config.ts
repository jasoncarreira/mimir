import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Minimal ambient typing so the dev-proxy can read MIMIR_DEV_API without pulling
// in @types/node (the config runs in Node, which provides the real `process`).
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react()],
  root: "frontend",
  base: "/app/",
  // Dev-only (ignored by `vite build`): `npm run dev` serves the app with HMR
  // and proxies the API to a running mimir so you iterate against real data +
  // auth + live events. Override the target with MIMIR_DEV_API (default is
  // mimirbot's host port-forward).
  server: {
    port: 5173,
    proxy: {
      // The chat bridge serves GET /chat/stream + POST /chat at the root (not
      // under /api), so proxy both prefixes or the chat stream 404s in dev.
      "/api": {
        target: process.env.MIMIR_DEV_API || "http://localhost:8090",
        changeOrigin: true
      },
      "/chat": {
        target: process.env.MIMIR_DEV_API || "http://localhost:8090",
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: "../mimir/react_app/dist",
    emptyOutDir: true,
    // Keep .lottie agent-character assets as real emitted files (not inlined
    // data: URIs) so <dotlottie-wc> fetches them as proper dotLottie ZIPs
    // (chainlink #565). Everything else uses Vite's default inline threshold.
    assetsInlineLimit: (filePath) => (filePath.endsWith(".lottie") ? false : undefined)
  }
});
