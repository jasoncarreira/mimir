import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  root: "frontend",
  base: "/app/",
  build: {
    outDir: "../mimir/react_app/dist",
    emptyOutDir: true,
    // Keep .lottie agent-character assets as real emitted files (not inlined
    // data: URIs) so <dotlottie-wc> fetches them as proper dotLottie ZIPs
    // (chainlink #565). Everything else uses Vite's default inline threshold.
    assetsInlineLimit: (filePath) => (filePath.endsWith(".lottie") ? false : undefined)
  }
});
