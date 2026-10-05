import type { EChartsCoreOption } from 'echarts/core'

import type { components } from '@/api/schema'
import { CHART_LIGHT, type ChartPalette } from '@/theme/tokens'

/**
 * 结果页三张图的 ECharts option 构建器。
 *
 * 全部是纯函数：输入契约生成的 RunDetail，输出 option 对象，不碰 DOM、不引 echarts 运行时
 * （只用 `import type`），因此可以被 S5.8 的 Vitest 直接单测。
 * 类型派生自 `@/api/schema`（openapi-typescript 生成物），禁止手写并行类型定义（AGENTS.md 第 2 节）。
 */
type RunDetail = components['schemas']['RunDetail']

/** 要素覆盖率目标（产品方案 §10.1：线索覆盖度 ≥ 60%）。 */
export const COVERAGE_TARGET = 0.6
/** 单热点成本预算（ADR 0012：≤ 0.15 元/热点）。 */
export const COST_BUDGET_PER_HOTSPOT_CNY = 0.15
/** 单热点耗时预算（产品方案 §10.1：≤ 60 秒/热点）。 */
export const LATENCY_BUDGET_PER_HOTSPOT_S = 60

/** run 级成本图的两个类目名（同时用作 series 名，tooltip 按它分支）。 */
const COST_SERIES = '成本（元）'
const LATENCY_SERIES = '耗时（秒）'

/** 配色对齐页面既有的 Tailwind 口径（slate / blue / violet / amber / red）。 */
// 颜色全部来自主题调色板（浅/深两套，见 src/theme/tokens.ts），图表跟随暗色切换

/** tooltip 回调的最小参数形状：显式声明，避免 strict 下的隐式 any。 */
interface TooltipItem {
  dataIndex?: number
  seriesName?: string
}

function toItems(params: unknown): TooltipItem[] {
  if (Array.isArray(params)) return params as TooltipItem[]
  return params === undefined || params === null ? [] : [params as TooltipItem]
}

function round1(value: number): number {
  return Math.round(value * 10) / 10
}

function round4(value: number): number {
  return Math.round(value * 10000) / 10000
}

/**
 * 覆盖度：按热点横向对比要素覆盖率，附 60% 目标线。
 * 数据源 `HotspotResult.coverage.ratio`；tooltip 补充已覆盖 / 缺口要素数。
 */
export function buildCoverageOption(run: RunDetail, palette: ChartPalette = CHART_LIGHT): EChartsCoreOption {
  const labels = run.hotspots.map((_, index) => `热点 ${index + 1}`)
  const ratios = run.hotspots.map((hotspot) => round1(hotspot.coverage.ratio * 100))
  const covered = run.hotspots.map((hotspot) => hotspot.coverage.covered.length)
  const gaps = run.hotspots.map((hotspot) => hotspot.coverage.gaps.length)

  return {
    aria: { enabled: true, description: '各热点要素覆盖率（百分比），虚线为 60% 目标' },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const index = toItems(params)[0]?.dataIndex ?? 0
        return `${labels[index]}<br/>覆盖率：${ratios[index]}%<br/>已覆盖要素：${covered[index]}<br/>缺口要素：${gaps[index]}`
      },
    },
    grid: { left: 8, right: 16, top: 24, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category',
      data: labels,
      axisTick: { alignWithLabel: true },
      axisLabel: { color: palette.axis },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: { formatter: '{value}%', color: palette.axis },
      splitLine: { lineStyle: { color: palette.splitLine } },
    },
    series: [
      {
        name: '要素覆盖率',
        type: 'bar',
        data: ratios,
        barMaxWidth: 48,
        itemStyle: { color: palette.bar, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', formatter: '{c}%', color: palette.label },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: palette.target, type: 'dashed', width: 1 },
          label: {
            formatter: `目标 ${Math.round(COVERAGE_TARGET * 100)}%`,
            position: 'insideEndTop',
            color: palette.targetLabel,
            fontSize: 11,
          },
          data: [{ yAxis: COVERAGE_TARGET * 100 }],
        },
      },
    ],
  }
}

/**
 * 候选素材得分分布：把本次运行全部候选的 score 归入 0–20 / 20–40 / 40–60 / 60–80 / 80–100 五档；
 * ≥ 60% 的档位用高亮色，对齐页面对候选 `score ≥ 0.6` 的既有口径。
 */
export function buildScoreDistributionOption(run: RunDetail, palette: ChartPalette = CHART_LIGHT): EChartsCoreOption {
  const labels = ['0–20%', '20–40%', '40–60%', '60–80%', '80–100%']
  const counts = [0, 0, 0, 0, 0]

  for (const hotspot of run.hotspots) {
    for (const candidate of hotspot.candidates) {
      const index = Math.min(labels.length - 1, Math.max(0, Math.floor(candidate.score * labels.length)))
      counts[index] += 1
    }
  }

  const data = counts.map((count, index) => ({
    value: count,
    itemStyle: {
      color: index >= 3 ? palette.high : palette.muted,
      borderRadius: [4, 4, 0, 0],
    },
  }))

  return {
    aria: { enabled: true, description: '候选素材得分分布（按得分档位计数的条形图）' },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const index = toItems(params)[0]?.dataIndex ?? 0
        return `得分 ${labels[index]}：${counts[index]} 条素材`
      },
    },
    grid: { left: 8, right: 16, top: 24, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category',
      data: labels,
      axisTick: { alignWithLabel: true },
      axisLabel: { color: palette.axis },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: palette.axis },
      splitLine: { lineStyle: { color: palette.splitLine } },
    },
    series: [
      {
        name: '候选素材数',
        type: 'bar',
        data,
        barMaxWidth: 48,
        label: { show: true, position: 'top', formatter: '{c}', color: palette.label },
      },
    ],
  }
}

/**
 * 成本与耗时：契约只有 run 级 totals（没有逐热点分解），因此按 run 总量呈现，
 * 叠加按热点数换算的预算线——单热点成本 0.15 元（ADR 0012）、单热点耗时 60 秒（产品方案 §10.1）。
 */
export function buildCostLatencyOption(run: RunDetail, palette: ChartPalette = CHART_LIGHT): EChartsCoreOption {
  const hotspotCount = Math.max(run.hotspots.length, 1)
  const costActual = round4(run.totals.cost_cny)
  const costBudget = round4(COST_BUDGET_PER_HOTSPOT_CNY * hotspotCount)
  const latencyActual = round1(run.totals.latency_ms / 1000)
  const latencyBudget = round1(LATENCY_BUDGET_PER_HOTSPOT_S * hotspotCount)

  function verdict(actual: number, budget: number): string {
    return actual <= budget ? '✓ 达标' : '✗ 超预算'
  }

  return {
    aria: {
      enabled: true,
      description: '本次运行的成本（元）与耗时（秒），虚线为按热点数换算的预算',
    },
    tooltip: {
      trigger: 'item',
      formatter: (params: unknown) => {
        const item = toItems(params)[0]
        if (item?.seriesName === COST_SERIES) {
          return `${COST_SERIES}<br/>本次运行：¥${costActual.toFixed(4)}<br/>预算（${COST_BUDGET_PER_HOTSPOT_CNY} 元 × ${hotspotCount} 热点）：¥${costBudget.toFixed(4)}<br/>单热点均值：¥${(costActual / hotspotCount).toFixed(4)}<br/>${verdict(costActual, costBudget)}`
        }
        return `${LATENCY_SERIES}<br/>本次运行：${latencyActual.toFixed(1)} 秒<br/>预算（${LATENCY_BUDGET_PER_HOTSPOT_S} 秒 × ${hotspotCount} 热点）：${latencyBudget.toFixed(1)} 秒<br/>单热点均值：${(latencyActual / hotspotCount).toFixed(1)} 秒<br/>${verdict(latencyActual, latencyBudget)}`
      },
    },
    grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category',
      data: [COST_SERIES, LATENCY_SERIES],
      axisTick: { alignWithLabel: true },
      axisLabel: { color: palette.axis },
    },
    yAxis: [
      {
        type: 'value',
        name: '元',
        min: 0,
        // ECharts 的轴范围不含 markLine 值：显式把预算线包进来，否则预算线会被顶到轴顶、与耗时线重叠
        max: (value: { max: number }) => Math.max(value.max, costBudget) * 1.15,
        axisLabel: { color: palette.axis },
        splitLine: { lineStyle: { color: palette.splitLine } },
      },
      {
        type: 'value',
        name: '秒',
        min: 0,
        max: (value: { max: number }) => Math.max(value.max, latencyBudget) * 1.15,
        axisLabel: { color: palette.axis },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: COST_SERIES,
        type: 'bar',
        yAxisIndex: 0,
        data: [costActual, null],
        barMaxWidth: 64,
        itemStyle: { color: palette.bar, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', formatter: `¥${costActual.toFixed(4)}`, color: palette.label },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: palette.target, type: 'dashed', width: 1 },
          label: {
            formatter: `预算 ¥${costBudget.toFixed(4)}`,
            position: 'insideStartTop',
            color: palette.targetLabel,
            fontSize: 11,
          },
          data: [{ yAxis: costBudget }],
        },
      },
      {
        name: LATENCY_SERIES,
        type: 'bar',
        yAxisIndex: 1,
        data: [null, latencyActual],
        barMaxWidth: 64,
        itemStyle: { color: palette.barAlt, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', formatter: `${latencyActual.toFixed(1)}s`, color: palette.label },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: palette.target, type: 'dashed', width: 1 },
          label: {
            formatter: `预算 ${latencyBudget.toFixed(1)}s`,
            position: 'insideEndTop',
            color: palette.targetLabel,
            fontSize: 11,
          },
          data: [{ yAxis: latencyBudget }],
        },
      },
    ],
  }
}
