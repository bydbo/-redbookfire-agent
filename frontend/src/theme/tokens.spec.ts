import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  BRAND,
  BRAND_GLASS,
  BRAND_STRONG,
  CONTRAST_PAIRS,
  DARK,
  LIGHT,
  type Palette,
} from '@/theme/tokens'

// ---- 与 S5.7 一次性校验脚本同一套算法，这里落成正式单测 ----

function toRgb(color: string): [number, number, number] {
  const value = color.trim()
  if (value.startsWith('#')) {
    const hex = value.slice(1)
    const step = hex.length === 3 ? 1 : 2
    const parts: number[] = []
    for (let i = 0; i < hex.length; i += step) {
      const chunk = hex.slice(i, i + step)
      parts.push(Number.parseInt(step === 1 ? chunk + chunk : chunk, 16))
    }
    return [parts[0], parts[1], parts[2]]
  }
  const parts = (value.match(/[\d.]+/g) ?? []).map(Number)
  return [parts[0], parts[1], parts[2]]
}

function luminance(color: string): number {
  const f = (channel: number): number => {
    const v = channel / 255
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  }
  const [r, g, b] = toRgb(color)
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

export function contrast(fg: string, bg: string): number {
  const a = luminance(fg)
  const b = luminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

/** 半透明前景叠到底色上的等效色（玻璃底的对比度必须按等效色算）。 */
function composite(rgba: string, background: string): string {
  const [r, g, b, a] = (rgba.match(/[\d.]+/g) ?? []).map(Number)
  const [br, bg_, bb] = toRgb(background)
  return `rgb(${a * r + (1 - a) * br}, ${a * g + (1 - a) * bg_}, ${a * b + (1 - a) * bb})`
}

describe('主题 token 对比度（S5.7 验收：暗色正文 ≥ 4.5:1）', () => {
  it.each(CONTRAST_PAIRS.map((pair) => [pair.label, pair] as const))(
    '%s 达到阈值',
    (_label, pair) => {
      expect(contrast(pair.fg, pair.bg)).toBeGreaterThanOrEqual(pair.min)
    },
  )

  it('暗色正文与次文本都明显高于 4.5', () => {
    expect(contrast(DARK.ink, DARK.surface)).toBeGreaterThan(10)
    expect(contrast(DARK.inkMuted, DARK.surface)).toBeGreaterThan(6)
  })

  it('主按钮玻璃底叠到白底后，白字仍达标', () => {
    const effective = composite(BRAND_GLASS, LIGHT.surface)
    expect(contrast('#ffffff', effective)).toBeGreaterThanOrEqual(4.5)
  })

  it('品牌红拆两档是有依据的：vivid 红配白字不达标，加深版达标', () => {
    expect(contrast(BRAND, '#ffffff')).toBeLessThan(4.5)          // 3.76:1
    expect(contrast(BRAND_STRONG, '#ffffff')).toBeGreaterThanOrEqual(4.5)
  })

  it('品牌红当红字放在深色底上可用', () => {
    expect(contrast(BRAND, DARK.surface)).toBeGreaterThanOrEqual(4.5)
  })
})

describe('theme.css 与 tokens.ts 同源（防两处字面值漂移）', () => {
  // 测试走 tsconfig.vitest.json（开了 node 类型），所以这里可以直接读源码文本；
  // jsdom 下 import.meta.url 不是 file: 协议，用 cwd 定位最稳。
  const css = readFileSync(resolve(process.cwd(), 'src/assets/theme.css'), 'utf8')

  function readVars(selector: string): Record<string, string> {
    const start = css.indexOf(selector)
    expect(start, `theme.css 里找不到 ${selector}`).toBeGreaterThanOrEqual(0)
    const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start))
    const out: Record<string, string> = {}
    for (const match of body.matchAll(/(--xhs-[a-z0-9-]+):\s*([^;]+);/g)) {
      out[match[1]] = match[2].trim()
    }
    return out
  }

  /** 归一化：去空格、小写、数值去掉多余的零（0.60 === 0.6）。 */
  function norm(color: string): string {
    return color.replace(/\s/g, '').toLowerCase().replace(/\d+\.\d+/g, (m) => String(Number(m)))
  }

  const MAPPING: Array<[keyof Palette, string]> = [
    ['surface', '--xhs-surface'],
    ['surfaceMuted', '--xhs-surface-muted'],
    ['glassBg', '--xhs-glass-bg'],
    ['glassHover', '--xhs-glass-hover'],
    ['glassPressed', '--xhs-glass-pressed'],
    ['glassBorder', '--xhs-glass-border'],
    ['inkStrong', '--xhs-ink-strong'],
    ['ink', '--xhs-ink'],
    ['inkMuted', '--xhs-ink-muted'],
    ['disabledInk', '--xhs-disabled-ink'],
    ['line', '--xhs-line'],
    ['brand', '--xhs-brand'],
    ['brandHover', '--xhs-brand-hover'],
    ['brandStrong', '--xhs-brand-strong'],
    ['onBrand', '--xhs-on-brand'],
    ['accent', '--xhs-accent'],
  ]

  it.each([
    [':root', LIGHT],
    ["html[data-theme='dark']", DARK],
  ] as const)('%s 的每个变量都与 tokens.ts 一致', (selector, palette) => {
    const vars = readVars(selector)
    for (const [field, cssVar] of MAPPING) {
      expect(vars[cssVar], `${cssVar} 缺失`).toBeDefined()
      expect(norm(vars[cssVar]), `${cssVar} 漂移`).toBe(norm(palette[field]))
    }
  })
})
