import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../hermes_cli/napcat_web_dist",
    emptyOutDir: true,
  },
  server: {
    port: 9121,
    proxy: {
      "/api": "http://127.0.0.1:9120",
      "/ws": { target: "ws://127.0.0.1:9120", ws: true },
    },
  },
});
