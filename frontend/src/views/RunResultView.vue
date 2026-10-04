<script setup lang="ts">
import { NAlert, NButton, NCard, NTag } from 'naive-ui'
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiRequestError, apiUrl } from '@/api/client'
import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'

const route = useRoute()
const runId = computed(() => String(route.params.runId))

const run = ref<RunDetail | null>(null)
const notFinished = ref(false)
const failure = ref<string | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  failure.value = null
  notFinished.value = false
  try {
    run.value = await getRun(runId.value)
  } catch (err) {
    if (err instanceof ApiRequestError && err.httpStatus === 409) {
      notFinished.value = true
    } else {
      failure.value = err instanceof Error ? err.message : '未知错误'
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
})

const statusMeta = computed(() => {
  switch (run.value?.status) {
    case 'succeeded':
      return { type: 'success' as const, label: '已完成' }
    case 'failed':
      return { type: 'error' as const, label: '失败' }
    case 'running':
      return { type: 'info' as const, label: '分析中' }
    case 'queued':
      return { type: 'warning' as const, label: '排队中' }
    default:
      return null
  }
})

const reportUrl = computed(() => apiUrl(`/runs/${runId.value}/report?format=html`))
</script>

<template>
  <main class="min-h-screen bg-slate-50 px-4 py-10">
    <div class="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <header class="flex items-center justify-between">
        <div>
          <h1 class="text-2xl font-bold text-slate-800">
            运行结果
          </h1>
          <p class="mt-1 text-xs text-slate-500">
            run_id：<code class="rounded bg-slate-100 px-1">{{ runId }}</code>
          </p>
        </div>
        <n-button size="small" @click="load">
          刷新
        </n-button>
      </header>

      <n-alert v-if="failure" type="error" :title="failure" />
      <n-alert v-else-if="notFinished" type="warning" title="运行尚未完成">
        结果写回后即可查看（409）。可稍后刷新，或回到分析台重新提交。
      </n-alert>

      <n-card v-if="run" title="运行概况">
        <div class="flex flex-col gap-3">
          <div class="flex items-center gap-3 text-sm">
            <n-tag v-if="statusMeta" :type="statusMeta.type" size="small">
              {{ statusMeta.label }}
            </n-tag>
            <span class="text-slate-600">创建于 {{ new Date(run.created_at).toLocaleString() }}</span>
          </div>
          <div class="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div class="rounded-lg bg-slate-100 p-3">
              <div class="text-xs text-slate-500">模型调用</div>
              <div class="text-lg font-semibold text-slate-800">{{ run.totals.llm_calls }}</div>
            </div>
            <div class="rounded-lg bg-slate-100 p-3">
              <div class="text-xs text-slate-500">耗时</div>
              <div class="text-lg font-semibold text-slate-800">{{ (run.totals.latency_ms / 1000).toFixed(1) }} 秒</div>
            </div>
            <div class="rounded-lg bg-slate-100 p-3">
              <div class="text-xs text-slate-500">成本</div>
              <div class="text-lg font-semibold text-slate-800">¥{{ run.totals.cost_cny.toFixed(4) }}</div>
            </div>
            <div class="rounded-lg bg-slate-100 p-3">
              <div class="text-xs text-slate-500">热点数</div>
              <div class="text-lg font-semibold text-slate-800">{{ run.hotspots.length }}</div>
            </div>
          </div>
          <div>
            <n-button tag="a" :href="reportUrl" target="_blank" type="primary" size="small">
              打开完整报告（HTML）
            </n-button>
          </div>
          <n-alert type="info" title="结果详情页（要素标签 / 候选素材 / 覆盖缺口 / 文案初稿）由 S5.4 落地">
            本页当前只提供运行概况与报告入口。
          </n-alert>
        </div>
      </n-card>
      <n-card v-else-if="loading" title="加载中…" />
    </div>
  </main>
</template>
