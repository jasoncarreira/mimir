import { defineConfig } from "vitest/config";
import { createRequire } from "node:module";
import { dirname } from "node:path";

const require = createRequire(import.meta.url);

// Minimal vitest config: register a global test setup that resets shared
// client state between cases (chainlink #564). Test files keep their per-file
// `// @vitest-environment jsdom` pragmas; this only adds setupFiles, leaving
// vitest's default include/exclude and environment behavior intact.
export default defineConfig({
  server: {
    fs: {
      // Worklink checkouts reuse the repository's dependency tree outside the
      // linked checkout. Allow the package that owns the WASM player asset.
      allow: [process.cwd(), dirname(require.resolve("@lottiefiles/dotlottie-web"))]
    }
  },
  test: {
    setupFiles: ["./frontend/src/vitest.setup.ts"],
  },
});
