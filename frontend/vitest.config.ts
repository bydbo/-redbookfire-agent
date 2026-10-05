import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 单测配置（S5.8）：jsdom 环境（组件要真挂载）、`@` 别名与 vite.config.ts 一致。
// 有意**不开 globals**：每个 spec 显式 `import { describe, it, expect } from 'vitest'`，
// 这样 `vue-tsc` 在 type-check / build 时会把测试代码一起做类型检查。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.spec.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text'],
      // 只报告不设门槛（S5.8 验收只要「覆盖工具函数与关键组件」）：
      // 生成物与纯引导文件不算分母。
      include: ['src/**/*.{ts,vue}'],
      exclude: ['src/api/schema.d.ts', 'src/main.ts', 'src/**/*.spec.ts'],
    },
  },
})
