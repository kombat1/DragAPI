import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// адрес бэкенда для режима разработки (start.sh dev задаёт его сам, если порт другой)
const backend = process.env.DRAGAPI_BACKEND || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.DRAGAPI_FRONT_PORT) || 5173,
    strictPort: true,
    // secure: false — бэкенд может работать на своём сертификате (--tls-self-signed)
    proxy: {
      '/api': { target: backend, ws: true, secure: false }, // ws — подключение удалённых агентов автоматизации
      '/run': { target: backend, secure: false },
      '/hooks': { target: backend, secure: false },
    },
  },
})
