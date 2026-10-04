import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Vite 配置（契约见 docs/contracts/配置契约.md §五）：
// - 路径别名 @ → src，与 tsconfig.json 的 paths 保持一致
// - 开发服务器默认 http://localhost:5173，/api 代理到 http://127.0.0.1:8000（后端无 CORS，ADR 0008）
// - 构建产物落 frontend/dist，由 FastAPI 挂载到根路径（ADR 0009）
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
