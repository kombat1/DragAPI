import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.DRAGAPI_BACKEND || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': backend, '/run': backend },
  },
})
