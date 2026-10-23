import { defineConfig } from "vite";
import { resolve } from "node:path";

export default defineConfig({
  root: resolve(import.meta.dirname),
  esbuild: { jsx: "transform", jsxFactory: "React.createElement", jsxFragment: "React.Fragment" },
  build: {
    emptyOutDir: false,
    outDir: resolve(import.meta.dirname, "../mimir_cultivate/frontend"),
    cssCodeSplit: false,
    rollupOptions: {
      input: resolve(import.meta.dirname, "src/main.tsx"),
      output: { entryFileNames: "cultivate.v1.js", assetFileNames: "cultivate.v1.[ext]", format: "es" }
    }
  }
});
