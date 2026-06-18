import { defineConfig } from "vitest/config";

// Minimal vitest config: register a global test setup that resets shared
// client state between cases (chainlink #564). Test files keep their per-file
// `// @vitest-environment jsdom` pragmas; this only adds setupFiles, leaving
// vitest's default include/exclude and environment behavior intact.
export default defineConfig({
  test: {
    setupFiles: ["./frontend/src/vitest.setup.ts"],
  },
});
