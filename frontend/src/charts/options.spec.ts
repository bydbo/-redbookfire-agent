import { describe, expect, it } from 'vitest'

import type { components } from '@/api/schema'
import {
  buildCostLatencyOption,
  buildCoverageOption,
  buildScoreDistributionOption,
} from '@/charts/options'
import { CHART_DARK, CHART_LIGHT } from '@/theme/tokens'

type RunDetail = components['schemas']['RunDetail']
type HotspotResult = components['schemas']['HotspotResult']

// ---- 只声明测试真正读到的字段：ECharts 的 option 类型本身很宽，这里窄读更安全 ----

interface TestAxis {
  max?: number | ((value: { max: number }) => number)
  axisLabel: { color: string }
}

interface TestSeries {
  data: Array<number | null | { value: number; itemStyle: { color: string } }>
  itemStyle?: { color: string }
  markLine?: { data: Array<{ yAxis: number }> }
}

interface TestOption {
  aria?: { enabled?: boolean }
  xAxis: { data: string[] }
  yAxis: TestAxis | TestAxis[]
  series: TestSeries[]
}

function asOption(option: unknown): TestOption {
  return option as TestOption
}

function axis(option: TestOption, index = 0): TestAxis {
  return Array.isArray(option.yAxis) ? option.yAxis[index] : option.yAxis
}

function series(option: TestOption, index = 0): TestSeries {
  return option.series[index]
}

/** 从可能带样式的数据项里取数值。 */
function valueOf(item: number | null | { value: number }): number | null {
  return typeof item === 'object' && item !== null ? item.value : item
}

function colorOf(item: number | null | { itemStyle: { color: string } }): string | undefined {
  return typeof item === 'object' && item !== null ? item.itemStyle.color : undefined
}

// ---- 夹具：契约要求的最小运行结果 ----

function element(value: string) {
  return { type: 'topic' as const, value, weight: 0.9, confidence: 0.9 }
}

function hotspot(raw: string, ratio: number, scores: number[], gaps = 0): HotspotResult {
  return {
    hotspot_id: crypto.randomUUID(),
    hotspot_raw: raw,
    clue: { why_it_works: ['反差'], mechanisms: [{ name: '反差' }], elements: [element('羽毛球')] },
    coverage: {
      ratio,
      covered: [element('羽毛球')],
      gaps: Array.from({ length: gaps }, (_, i) => element(`缺口${i}`)),
    },
    candidates: scores.map((score, index) => ({
      rank: index + 1,
      material_id: crypto.randomUUID(),
      score,
      recall_sources: ['literal' as const],
      hits: [{ element_type: 'topic' as const, clue_value: '羽毛球' }],
      missing: [],
      reasons: ['命中主题'],
      usage: '放开头 3 秒',
      material: {
        id: crypto.randomUUID(),
        path: `D:/materials/片段-${index}.mp4`,
        type: 'video' as const,
        title: `片段 ${index}`,
        keyframes: [],
      },
    })),
    draft: null,
  }
}

const RUN: RunDetail = {
  run_id: '8860c661-bccb-4246-ba8b-1da6bb63f5de',
  status: 'succeeded',
  created_at: '2026-10-04T20:23:22+00:00',
  finished_at: '2026-10-04T20:25:05+00:00',
  totals: {
    llm_calls: 6,
    prompt_tokens: 8375,
    completion_tokens: 8614,
    cost_cny: 0.2202,
    latency_ms: 102869,
  },
  prompt_versions: { hotspot_clue: 1 },
  hotspots: [
    hotspot('热点一', 1, [0.954, 0.949, 0.94, 0.923, 0.885]),
    hotspot('热点二', 1, [0.935, 0.913, 0.907, 0.905, 0.303]),
  ],
}

describe('buildCoverageOption', () => {
  const option = asOption(buildCoverageOption(RUN))

  it('每个热点一根柱，取值是覆盖率百分比', () => {
    expect(option.xAxis.data).toEqual(['热点 1', '热点 2'])
    expect(series(option).data).toEqual([100, 100])
  })

  it('画 60% 目标线，且轴上限固定 100 避免柱子顶格', () => {
    expect(series(option).markLine?.data[0].yAxis).toBe(60)
    expect(axis(option).max).toBe(100)
    expect(option.aria?.enabled).toBe(true)
  })
})

describe('buildScoreDistributionOption', () => {
  it('按五档分箱计数，1.0 落在最后一档（不能越界）', () => {
    const run: RunDetail = { ...RUN, hotspots: [hotspot('边界', 1, [0, 0.2, 0.6, 0.999, 1])] }
    const option = asOption(buildScoreDistributionOption(run))
    // floor(score × 5) 分箱：0→0 档、0.2→1 档、0.6→3 档、0.999 与 1.0 都落 4 档
    expect(series(option).data.map(valueOf)).toEqual([1, 1, 0, 1, 2])
  })

  it('高分档用高亮色、低分档用弱化色', () => {
    const option = asOption(buildScoreDistributionOption(RUN))
    const colors = series(option).data.map(colorOf)
    expect(colors.slice(0, 3)).toEqual([CHART_LIGHT.muted, CHART_LIGHT.muted, CHART_LIGHT.muted])
    expect(colors[3]).toBe(CHART_LIGHT.high)
    expect(colors[4]).toBe(CHART_LIGHT.high)
  })
})

describe('buildCostLatencyOption', () => {
  const option = asOption(buildCostLatencyOption(RUN))

  it('成本与耗时取 run 级 totals，双轴各自成图', () => {
    expect(option.xAxis.data).toEqual(['成本（元）', '耗时（秒）'])
    expect(valueOf(series(option, 0).data[0])).toBe(0.2202)
    expect(valueOf(series(option, 1).data[1])).toBe(102.9)
  })

  it('预算线按热点数换算（0.15 元 / 60 秒 × 2 热点）', () => {
    expect(series(option, 0).markLine?.data[0].yAxis).toBe(0.3)
    expect(series(option, 1).markLine?.data[0].yAxis).toBe(120)
  })

  it('轴上限显式包住预算线（S5.5 踩过的坑：否则预算线会被顶到轴顶）', () => {
    const costMax = axis(option, 0).max
    const latencyMax = axis(option, 1).max
    expect(typeof costMax).toBe('function')
    expect(typeof latencyMax).toBe('function')
    const asFn = (fn: number | ((value: { max: number }) => number) | undefined) =>
      fn as (value: { max: number }) => number
    expect(asFn(costMax)({ max: RUN.totals.cost_cny })).toBeGreaterThan(0.3)
    expect(asFn(latencyMax)({ max: RUN.totals.latency_ms / 1000 })).toBeGreaterThan(120)
  })

  it('没有热点时按 1 个热点兜底，不出现除零', () => {
    const empty: RunDetail = { ...RUN, hotspots: [] }
    const only = asOption(buildCostLatencyOption(empty))
    expect(series(only, 0).markLine?.data[0].yAxis).toBe(0.15)
    expect(series(only, 1).markLine?.data[0].yAxis).toBe(60)
  })
})

describe('调色板参数（图表跟随主题，S5.7）', () => {
  it('默认浅色；传入暗色调色板时轴色与柱色随之改变', () => {
    const light = asOption(buildCoverageOption(RUN))
    const dark = asOption(buildCoverageOption(RUN, CHART_DARK))
    expect(axis(light).axisLabel.color).toBe(CHART_LIGHT.axis)
    expect(axis(dark).axisLabel.color).toBe(CHART_DARK.axis)
    expect(series(dark).itemStyle?.color).toBe(CHART_DARK.bar)
  })
})
