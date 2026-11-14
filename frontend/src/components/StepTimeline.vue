<script setup lang="ts">
// 工具阶段时间线（纯展示）：把 `store.toolSteps` 画成「已完成的给耗时 / 当前的标红 / 失败的说不算数」。
// 不含折叠与摘要——那些属于所在气泡，这里只认 props。
import { computed, onUnmounted, ref, watch } from 'vue'

import type { ToolStep } from '@/stores/chat'
import { formatElapsed } from '@/utils/format'

const props = defineProps<{ steps: ToolStep[] }>()

// 「进行中」的耗时每秒重算一次。只在真有待跑阶段时开定时器，跑完立刻停——
// 不用 store 的全局 clock，避免一个纯展示组件影响别处的节拍。
const now = ref(Date.now())
let timer: number | undefined
const hasRunning = computed(() => props.steps.some((step) => step.status === 'running'))

watch(hasRunning, (running) => {
  if (running) {
    now.value = Date.now()
    timer = window.setInterval(() => { now.value = Date.now() }, 1000)
  } else if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}, { immediate: true })

onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer)
})

function rowClass(step: ToolStep): string {
  if (step.status === 'succeeded') return 'is-done'
  if (step.status === 'failed') return 'is-failed'
  return 'is-active'
}

function iconOf(step: ToolStep): string {
  if (step.status === 'succeeded') return '✓'
  if (step.status === 'failed') return '✕'
  return '●'
}

function metaOf(step: ToolStep): string {
  if (step.status === 'running') return `进行中 · ${formatElapsed(now.value - step.startedAt)}`
  if (step.status === 'failed') return '未完成'
  return formatElapsed((step.endedAt ?? now.value) - step.startedAt)
}
</script>

<template>
  <ol class="step-timeline flex flex-col">
    <li
      v-for="(step, index) in steps"
      :key="`${index}-${step.stage}`"
      class="step-row"
      :class="rowClass(step)"
    >
      <span class="step-icon" aria-hidden="true">{{ iconOf(step) }}</span>
      <span class="step-name">{{ step.stage }}</span>
      <span class="step-meta">{{ metaOf(step) }}</span>
    </li>
  </ol>
</template>

<style scoped>
.step-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0 3px 22px;
}

/* 竖向连接线：首行从图标起、末行到图标止，避免线头露在两端 */
.step-row::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--xhs-line);
}

.step-row:first-child::before { top: 10px; }
.step-row:last-child::before { bottom: auto; height: 10px; }

.step-icon {
  position: absolute;
  left: 0;
  top: 3px;
  width: 14px;
  height: 14px;
  border-radius: 9999px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  line-height: 1;
  background: var(--xhs-surface);
}

.step-name { font-size: 12px; }
.step-meta { margin-left: auto; font-size: 11px; font-variant-numeric: tabular-nums; }

.is-done .step-icon { color: var(--xhs-accent); }
.is-done .step-name,
.is-done .step-meta { color: var(--xhs-ink-muted); }

.is-active .step-icon { color: var(--xhs-brand); }
.is-active .step-name { color: var(--xhs-ink-strong); font-weight: 600; }
.is-active .step-meta { color: var(--xhs-brand); }

.is-failed .step-icon,
.is-failed .step-meta { color: var(--xhs-brand-strong); }
.is-failed .step-name { color: var(--xhs-ink-strong); font-weight: 600; }
</style>
