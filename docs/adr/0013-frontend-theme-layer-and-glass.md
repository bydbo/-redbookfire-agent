# ADR 0013 · 前端主题层与毛玻璃的样式边界

- 状态：已采纳
- 日期：2026-10-05
- 关联：`docs/adr/0008-frontend-vue-vite.md`、`docs/backlog.md` 的 S5.7、`frontend/src/theme/`、`frontend/src/assets/theme.css`

## 背景

S5.7 要求「三页共用同一套主题 token」，按钮 / 顶栏 / 卡片要有磨玻璃透明质感，还要能切暗色。
ADR 0008 定的边界是「Tailwind 管布局与视觉定制，Naive UI 管组件，**禁止用 Tailwind 覆盖 Naive 内部结构**」；
但 `backdrop-filter` 无法通过 Naive 的 `themeOverrides` 表达（那是 CSS 属性，不是主题变量），
于是只有三条路：违反 0008 用工具类覆盖、为每个按钮包一层自研组件、或者显式定义一个受控的例外。

## 决策

- **颜色单一来源**：`frontend/src/theme/tokens.ts`（字面值表）→ `frontend/src/assets/theme.css` 的 CSS 变量 →
  Tailwind 语义色（`surface` / `ink` / `line` / `brand` …）。Naive 的 `themeOverrides` 由同一份 token 生成，
  并**写死字面值**：Naive 内部会用 JS 派生 hover / pressed 颜色，读不懂 `var()`。
- **毛玻璃走主题样式层**：`assets/theme.css` 只对 `.n-button` / `.n-card` / 顶栏**新增**
  `backdrop-filter`、半透明底与细描边；不改组件结构、不改布局，颜色 / 圆角 / 阴影仍归 `themeOverrides`。
  这是 ADR 0008「不许覆盖 Naive 内部结构」的**唯一例外**，且限定为「只加属性、不改结构」。
- **品牌红拆两档**：`--xhs-brand:#ff2442`（装饰 / 大面积 / 暗色底上的红字）与
  `--xhs-brand-strong:#d81e3c`（需要载白字的按钮底）。依据：`#FF2442` 配白字仅 3.76:1（不达 AA），
  加深版以 0.92 透明度叠加到白底后为 4.62:1、暗色内容上更高。
- **暗色只切变量**：页面不写 `dark:` 变体；默认跟随 `prefers-color-scheme`，手动切换写
  `localStorage['xhs-theme']`，首帧前由 `index.html` 的内联脚本定主题，避免白闪。

## 后果

**正面**：S6.5 新增第四页只需使用语义类；换品牌色 / 换肤只改一张表；对比度可被脚本逐对校验（已随 S5.7 落地）。

**负面**：多了一层「token → CSS 变量 → Tailwind」的间接；`tokens.ts` 与 `theme.css` 是两处字面值，
必须靠校验比对防漂移（S5.7 用一次性脚本核对，S5.8 应落成 Vitest 用例）。

**已知不做（记录在案，不 hack 组件库）**：Naive 的 `n-card-header` 带 `role="heading"` 却没有 `aria-level`，
`NDynamicInput` / `NInputNumber` 的图标按钮没有可访问名——这是组件库内部标记，Lighthouse 无障碍项因此扣分
（S5.7 实测 a11y 88，其中 `color-contrast` 满分）。要修得等 Naive 上游或改用自研控件，本次不做。
