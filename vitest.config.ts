import { configDefaults, defineConfig } from "vitest/config";
import { createRequire } from "node:module";
import { dirname } from "node:path";

const require = createRequire(import.meta.url);

// Register a global test setup that resets shared client state between cases
// (chainlink #564). Test files keep their per-file `// @vitest-environment jsdom`
// pragmas; use the default include/environment and exclude nested factory
// checkouts in addition to Vitest's default exclusions.
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
    exclude: [...configDefaults.exclude, "**/.factory/**", "**/.factory-sandboxes/**"],
  },
});
