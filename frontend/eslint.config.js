import js from '@eslint/js'
import vue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

// 前端 lint（S5.9）：只做**正确性**检查——不引入 Prettier 类格式化规则，
// 避免和仓库既有的样式口径打架（模板里的写法归 S5.7 的主题层与人工评审）。
// 生成物 `src/api/schema.d.ts` 与构建产物不进 lint 范围。
export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**', 'src/api/schema.d.ts'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        // <script setup lang="ts"> 要用 TS 解析器，否则模板里的类型语法会报解析错误
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
      },
    },
  },
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: { globals: { ...globals.browser } },
    rules: {
      // App.vue 是根壳层、EChart 是 ECharts 的固定拼写：单字名是刻意选择
      'vue/multi-word-component-names': ['error', { ignores: ['App', 'EChart'] }],
      'vue/no-unused-components': 'error',
    },
  },
  {
    // 构建/测试/lint 自身跑在 Node 里
    files: ['vite.config.ts', 'vitest.config.ts', 'eslint.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
)
