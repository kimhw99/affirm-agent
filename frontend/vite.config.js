import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development, /api goes to the gateway on this machine, as nginx does in Docker Compose.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.GATEWAY_URL ?? "http://localhost:8080",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.js",
  },
});
