/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 同源开发代理：本地 Python 服务监听 127.0.0.1:8765。
// 生产环境由后端同源托管 dist，构建产物使用相对路径 /api/v1。
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5273,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    globals: false,
  },
});