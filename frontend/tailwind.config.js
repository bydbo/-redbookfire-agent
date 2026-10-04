/** @type {import('tailwindcss').Config} */
// Tailwind 只负责布局与视觉定制，组件交互样式归 Naive UI（样式边界见 ADR 0008）
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {},
  },
  plugins: [],
}
