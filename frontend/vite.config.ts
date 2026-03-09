import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-ag-grid": ["ag-grid-community", "ag-grid-react"],
          "vendor-charts": ["recharts"],
          "vendor-three": ["three"],
          "vendor-ui": ["lucide-react", "@tabler/icons-react"],
        },
      },
    },
  },
  server: {
    allowedHosts: [
      "localhost",
      "127.0.0.1",
      "analysis.kohleservices.com",
      "10.6.20.138"
    ],
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET || "http://localhost:8001",
        changeOrigin: true,
      },
    },
  },
})
