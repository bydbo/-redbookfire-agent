/** @type {import('tailwindcss').Config} */
// Tailwind 只负责布局与视觉定制，组件交互样式归 Naive UI（样式边界见 ADR 0008 / 0013）。
// 颜色一律走语义 token：值来自 src/assets/theme.css 的 CSS 变量（与 src/theme/tokens.ts 同源），
// 暗色只切换变量，页面不写 dark: 变体。
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        surface: 'var(--xhs-surface)',
        'surface-muted': 'var(--xhs-surface-muted)',
        glass: 'var(--xhs-glass-bg)',
        ink: 'var(--xhs-ink)',
        'ink-strong': 'var(--xhs-ink-strong)',
        'ink-muted': 'var(--xhs-ink-muted)',
        line: 'var(--xhs-line)',
        brand: 'var(--xhs-brand)',
        'brand-strong': 'var(--xhs-brand-strong)',
        accent: 'var(--xhs-accent)',
      },
    },
  },
  plugins: [],
}
