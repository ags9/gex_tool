import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Ports mirror gexbot.config.Settings. Nothing here binds anything but
// loopback: this box holds brokerage credentials, and remote viewing means a
// tunnel, not a bind address (CLAUDE.md §14).
const API_PORT = process.env.GEX_API_PORT ?? "8742";
const UI_PORT = Number(process.env.GEX_DASHBOARD_PORT ?? 8741);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: UI_PORT,
    strictPort: true,
    proxy: {
      "/api": { target: `http://127.0.0.1:${API_PORT}` },
      "/ws": { target: `ws://127.0.0.1:${API_PORT}`, ws: true },
    },
  },
});
