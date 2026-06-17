import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  root: "frontend",
  base: "/app/",
  build: {
    outDir: "../mimir/react_app/dist",
    emptyOutDir: true
  }
});
