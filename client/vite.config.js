import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies API + WebSocket traffic to the aiohttp server (docs/CLIENT.md).
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': 'http://localhost:80',
      '/ws': { target: 'ws://localhost:80', ws: true },
    },
  },
})
