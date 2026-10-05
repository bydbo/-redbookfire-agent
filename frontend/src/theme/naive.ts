import type { GlobalThemeOverrides } from 'naive-ui'

import { BRAND_GLASS, BRAND_GLASS_HOVER, BRAND_GLASS_PRESSED, type Palette } from '@/theme/tokens'

/**
 * 由 token 表生成 Naive UI 的 themeOverrides（S5.7）。
 *
 * 这里刻意写**字面值**而不是 `var(--xhs-*)`：Naive 内部会用 JS 对颜色做派生（hover / pressed），
 * 读不懂 CSS 变量。CSS 变量负责 Tailwind 侧与毛玻璃层（见 assets/theme.css），两者由校验脚本比对。
 */
export function buildThemeOverrides(p: Palette): GlobalThemeOverrides {
  return {
    common: {
      primaryColor: p.brandStrong,
      primaryColorHover: p.brandHover,
      primaryColorPressed: p.brandStrong,
      primaryColorSuppl: p.brandHover,
      bodyColor: p.surface,
      cardColor: p.glassBg,
      modalColor: p.surface,
      popoverColor: p.surface,
      textColorBase: p.inkStrong,
      textColor1: p.inkStrong,
      textColor2: p.ink,
      textColor3: p.inkMuted,
      textColorDisabled: p.disabledInk,
      borderColor: p.line,
      dividerColor: p.line,
      borderRadius: '12px',
      borderRadiusSmall: '8px',
    },
    Button: {
      // 默认（次级）按钮 = 毛玻璃 + 深色字（浅色主题）/ 浅色字（暗色主题）
      color: p.glassBg,
      colorHover: p.glassHover,
      colorPressed: p.glassPressed,
      colorFocus: p.glassBg,
      colorDisabled: p.glassBg,
      textColor: p.inkStrong,
      textColorHover: p.inkStrong,
      textColorPressed: p.inkStrong,
      textColorFocus: p.inkStrong,
      textColorDisabled: p.disabledInk,
      border: `1px solid ${p.glassBorder}`,
      borderHover: `1px solid ${p.brand}`,
      borderPressed: `1px solid ${p.brandStrong}`,
      borderFocus: `1px solid ${p.brand}`,
      borderDisabled: `1px solid ${p.line}`,
      // primary = 品牌红毛玻璃底 + 白字（0.92 透明叠加后仍满足 AA，见 tokens.ts）
      colorPrimary: BRAND_GLASS,
      colorPrimaryHover: BRAND_GLASS_HOVER,
      colorPrimaryPressed: BRAND_GLASS_PRESSED,
      colorPrimaryFocus: BRAND_GLASS_HOVER,
      textColorPrimary: p.onBrand,
      textColorPrimaryHover: p.onBrand,
      textColorPrimaryPressed: p.onBrand,
      textColorPrimaryFocus: p.onBrand,
      borderPrimary: '1px solid rgba(255, 255, 255, 0.28)',
      borderPrimaryHover: '1px solid rgba(255, 255, 255, 0.36)',
      borderPrimaryPressed: '1px solid rgba(255, 255, 255, 0.36)',
      borderRadiusTiny: '8px',
      borderRadiusSmall: '8px',
      borderRadiusMedium: '10px',
      fontWeight: '500',
    },
    Card: {
      color: p.glassBg,
      borderColor: p.line,
      titleTextColor: p.inkStrong,
      titleFontWeight: '600',
      textColor: p.ink,
      borderRadius: '16px',
    },
    Input: {
      color: p.glassBg,
      colorFocus: p.glassHover,
      textColor: p.inkStrong,
      placeholderColor: p.inkMuted,
      caretColor: p.brandStrong,
      border: `1px solid ${p.glassBorder}`,
      borderHover: `1px solid ${p.brand}`,
      borderFocus: `1px solid ${p.brand}`,
      boxShadowFocus: `0 0 0 2px ${p.pageGlow}`,
    },
    Tag: {
      // 只改默认标签；success / warning / error 那几种仍走语义色（页面靠它们表达状态）
      color: p.glassBg,
      textColor: p.ink,
      border: `1px solid ${p.line}`,
      borderRadius: '8px',
    },
    Tooltip: {
      color: p.tooltipBg,
      textColor: p.tooltipInk,
      borderRadius: '10px',
    },
    Progress: {
      fillColor: p.brandStrong,
      railColor: p.line,
    },
    Pagination: {
      itemColor: 'transparent',
      itemColorHover: p.glassHover,
      itemColorActive: BRAND_GLASS,
      itemTextColor: p.ink,
      itemTextColorHover: p.inkStrong,
      itemTextColorActive: p.onBrand,
      itemBorder: `1px solid ${p.line}`,
      itemBorderActive: `1px solid ${p.brandStrong}`,
      itemBorderRadius: '10px',
    },
    Empty: {
      textColor: p.inkMuted,
      iconColor: p.inkMuted,
    },
  }
}
