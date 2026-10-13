import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Собранный бандл кладётся внутрь Python-пакета (flashhunter/ui_static) —
// так API-сервер отдаёт UI из одного процесса: docker-образ и локальный запуск идентичны.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../python/flashhunter/ui_static',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1400,
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ['echarts', 'echarts-for-react'],
          vendor: ['react', 'react-dom', 'react-router-dom', 'framer-motion'],
        },
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
      '/metrics': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
});
