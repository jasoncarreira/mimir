import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";


// Build-time fallback for the app shell's version label: the mimir package
// version (pyproject). The runtime bootstrap's `version` wins when present; this
// keeps the BUILD label populated offline / before the backend carries it.
function mimirVersion(): string {
  try {
    const toml = readFileSync("pyproject.toml", "utf-8");
    return /^version\s*=\s*"([^"]+)"/m.exec(toml)?.[1] ?? "";
  } catch {
    return "";
  }
}

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(mimirVersion())
  },
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
