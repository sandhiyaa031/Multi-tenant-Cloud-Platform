import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on :8000; proxying keeps the browser on one origin.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8000" } },
});
