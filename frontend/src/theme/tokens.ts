/**
 * 主题 token 单一来源（S5.7，边界见 ADR 0013）。
 *
 * 颜色取自小红书官网与其线上 CSS 包（一等源，2026-10-05 抓取）：
 * 品牌红 `#ff2442`（另有 hover 用的 `#ff2e4d`）、近黑底 `#19191e`、浅灰 `#f5f5f5`、
 * 品牌浅底写法 `rgba(255,36,66,.06)`、描边 `rgba(0,0,0,.08)`，以及其自带文本变量
 * `--color-primary-label:#333` / secondary `rgba(51,51,51,.8)` / tertiary `.6`。
 *
 * 品牌红拆两档（WCAG 实测）：
 * - `brand` `#ff2442`：装饰、大面积色块、图表、暗色底上的红字（配 `#19191e` = 4.66:1）。
 * - `brandStrong` `#d81e3c`：需要载白字的按钮底。直接用 `#ff2442` 配白字只有 **3.76:1**（不达标）；
 *   加深版以 0.92 透明度叠加后配白字 = **4.62:1**，达标。
 *
 * 命名按**语义角色**（不是颜色本身）：S6.5 新增第四页时只加语义类，不改变量名。
 */

export const BRAND = '#ff2442'
export const BRAND_HOVER = '#ff2e4d'
export const BRAND_STRONG = '#d81e3c'
/** 主按钮的毛玻璃底：品牌红加深版 + 0.92 透明（叠加后仍满足白字 AA）。 */
export const BRAND_GLASS = 'rgba(216, 30, 60, 0.92)'
export const BRAND_GLASS_HOVER = 'rgba(216, 30, 60, 0.96)'
export const BRAND_GLASS_PRESSED = 'rgba(198, 24, 52, 0.96)'
export const ON_BRAND = '#ffffff'

/** 一套主题要用到的全部语义色；浅色 / 深色各一份。 */
export interface Palette {
  surface: string
  surfaceMuted: string
  glassBg: string
  glassHover: string
  glassPressed: string
  glassBorder: string
  inkStrong: string
  ink: string
  inkMuted: string
  disabledInk: string
  line: string
  brand: string
  brandHover: string
  brandStrong: string
  onBrand: string
  accent: string
  pageGlow: string
  pageGlowAlt: string
  tooltipBg: string
  tooltipInk: string
}

export const LIGHT: Palette = {
  surface: '#ffffff',
  surfaceMuted: '#f5f5f5',
  glassBg: 'rgba(255, 255, 255, 0.66)',
  glassHover: 'rgba(255, 255, 255, 0.82)',
  glassPressed: 'rgba(255, 255, 255, 0.92)',
  glassBorder: 'rgba(255, 255, 255, 0.60)',
  inkStrong: '#19191e',
  ink: '#333333',
  inkMuted: '#6b6b73',
  disabledInk: 'rgba(51, 51, 51, 0.36)',
  line: 'rgba(0, 0, 0, 0.08)',
  brand: BRAND,
  brandHover: BRAND_HOVER,
  brandStrong: BRAND_STRONG,
  onBrand: ON_BRAND,
  accent: '#157d6b',
  pageGlow: 'rgba(255, 36, 66, 0.10)',
  pageGlowAlt: 'rgba(21, 125, 107, 0.08)',
  tooltipBg: '#19191e',
  tooltipInk: '#f5f5f5',
}

export const DARK: Palette = {
  surface: '#19191e',
  surfaceMuted: '#202027',
  glassBg: 'rgba(32, 32, 39, 0.62)',
  glassHover: 'rgba(38, 38, 46, 0.78)',
  glassPressed: 'rgba(44, 44, 53, 0.90)',
  glassBorder: 'rgba(255, 255, 255, 0.14)',
  inkStrong: '#f5f5f5',
  ink: '#dbdbdf',
  inkMuted: '#a9a9b3',
  disabledInk: 'rgba(219, 219, 223, 0.34)',
  line: 'rgba(255, 255, 255, 0.12)',
  brand: BRAND,
  brandHover: BRAND_HOVER,
  brandStrong: BRAND_STRONG,
  onBrand: ON_BRAND,
  accent: '#56d1bf',
  pageGlow: 'rgba(255, 36, 66, 0.16)',
  pageGlowAlt: 'rgba(86, 209, 191, 0.10)',
  tooltipBg: '#f5f5f5',
  tooltipInk: '#19191e',
}

/** 图表（ECharts）用的调色板：跟随主题切换，暗色下保证坐标轴/网格线可读。 */
export interface ChartPalette {
  bar: string
  barAlt: string
  high: string
  muted: string
  axis: string
  splitLine: string
  target: string
  targetLabel: string
  label: string
}

export const CHART_LIGHT: ChartPalette = {
  bar: BRAND,
  barAlt: '#157d6b',
  high: BRAND_STRONG,
  muted: '#9a9aa2',
  axis: '#6b6b73',
  splitLine: 'rgba(0, 0, 0, 0.10)',
  target: '#b45309',
  targetLabel: '#92400e',
  label: '#333333',
}

export const CHART_DARK: ChartPalette = {
  bar: BRAND,
  barAlt: '#56d1bf',
  high: BRAND_HOVER,
  muted: '#7b7b86',
  axis: '#a9a9b3',
  splitLine: 'rgba(255, 255, 255, 0.14)',
  target: '#fbbf24',
  targetLabel: '#fcd34d',
  label: '#dbdbdf',
}

/**
 * 需要在验收里逐对校验的「文字 × 底色」组合（theme.css 的变量要与这里一一对应）。
 * S5.8 会把这张表变成正式单测；本次先用一次性脚本核对。
 */
export const CONTRAST_PAIRS: ReadonlyArray<{
  label: string
  fg: string
  bg: string
  min: number
}> = [
  { label: '浅色正文 / 卡片底', fg: LIGHT.ink, bg: LIGHT.surface, min: 4.5 },
  { label: '浅色正文 / 浅灰底', fg: LIGHT.ink, bg: LIGHT.surfaceMuted, min: 4.5 },
  { label: '浅色次文本 / 卡片底', fg: LIGHT.inkMuted, bg: LIGHT.surface, min: 4.5 },
  // 品牌玻璃底 0.92 叠加到白底后的等效色（#db304b），必须按等效色核算
  { label: '浅色主按钮白字 / 品牌玻璃底', fg: ON_BRAND, bg: 'rgb(219, 48, 76)', min: 4.5 },
  { label: '暗色正文 / 深色底', fg: DARK.ink, bg: DARK.surface, min: 4.5 },
  { label: '暗色正文 / 深色卡片', fg: DARK.ink, bg: DARK.surfaceMuted, min: 4.5 },
  { label: '暗色次文本 / 深色底', fg: DARK.inkMuted, bg: DARK.surface, min: 4.5 },
  { label: '暗色强文本 / 深色底', fg: DARK.inkStrong, bg: DARK.surface, min: 4.5 },
  { label: '品牌红字 / 深色底', fg: BRAND, bg: DARK.surface, min: 4.5 },
]
