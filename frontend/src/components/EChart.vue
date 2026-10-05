<script setup lang="ts">
import type { EChartsCoreOption } from 'echarts/core'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { echarts } from '@/charts/echarts'

/**
 * ECharts 薄封装（S5.5）：负责真实图表的生命周期，option 由调用方用纯函数构建。
 * - 挂载时 init，option 变化时 setOption(notMerge) 全量替换（避免上次的 series 残留）
 * - ResizeObserver 跟随容器宽度自适应
 * - 卸载时解绑并 dispose，防止内存泄漏
 */
const props = withDefaults(defineProps<{ option: EChartsCoreOption; height?: string }>(), {
  height: '260px',
})

const container = ref<HTMLDivElement | null>(null)
let chart: ReturnType<typeof echarts.init> | null = null
let observer: ResizeObserver | null = null

function render(): void {
  chart?.setOption(props.option, { notMerge: true })
}

onMounted(() => {
  if (container.value === null) return
  chart = echarts.init(container.value)
  render()
  // 初次 observe 也会触发一次回调，顺带修正首帧尺寸（卡片内宽度确定后）
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(container.value)
})

watch(() => props.option, render, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <!-- 高度由 props 决定，宽度交给容器（Tailwind 只管外框，不覆盖 Naive UI 内部结构，ADR 0008） -->
  <div ref="container" :style="{ width: '100%', height }" />
</template>
