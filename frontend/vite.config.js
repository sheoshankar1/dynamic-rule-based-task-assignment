import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is proxied rather than called cross-origin, so no CORS package is
// needed on the Django side. BACKEND is overridden in docker-compose.
const BACKEND = process.env.BACKEND_URL || "http://localhost:8000";
const proxied = [
  "/auth", "/tasks", "/my-eligible-tasks", "/schema", "/docs",
  "/static",   // Swagger UI assets, served by Django not Vite
];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      proxied.map((path) => [path, { target: BACKEND, changeOrigin: true }])
    ),
  },
});
