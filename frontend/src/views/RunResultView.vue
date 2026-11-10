<script setup lang="ts">
import { NAlert, NButton, NCard, NTag } from 'naive-ui'
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiRequestError, apiUrl } from '@/api/client'
import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import HotspotResult from '@/components/HotspotResult.vue'
import RunCharts from '@/components/RunCharts.vue'

const route = useRoute()
const runId = computed(() => String(route.params.runId))

const EVIDENCE_ORIGIN_LABELS: Record<string, string> = {
  runtime: '运行时保存',
  legacy_run_record: '原运行记录',
  migration_capture: 'E6 迁移归档',
  cutover_capture: '切换时采集',
  missing: '缺失',
}

type SnapshotView = {
  llm?: { provider?: string; model?: string }
  retrieval?: { topk?: number }
}
const snapshot = computed(() => (run.value?.execution_snapshot ?? {}) as SnapshotView)
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
  <main class="px-4 py-5">
    <div class="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <header class="flex items-center justify-between">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">
            运行结果
          </h1>
          <p class="mt-1 text-xs text-ink-muted">
            run_id：<code class="rounded bg-surface-muted px-1">{{ runId }}</code>
          </p>
        </div>
        <n-button size="small" @click="load">
          刷新
        </n-button>
      </header>

      <n-alert v-if="failure" type="error" :title="failure" />
      <n-alert v-else-if="notFinished" type="warning" title="运行尚未完成">
        结果写回后即可查看（409）。可到运行历史查看，或稍后刷新重试。
      </n-alert>

      <n-card v-if="run" title="运行概况">
        <div class="flex flex-col gap-3">
          <div class="flex items-center gap-3 text-sm">
            <n-tag v-if="statusMeta" :type="statusMeta.type" size="small">
              {{ statusMeta.label }}
            </n-tag>
            <span class="text-ink">创建于 {{ new Date(run.created_at).toLocaleString() }}</span>
          </div>
          <div class="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div class="rounded-lg bg-surface-muted p-3">
              <div class="text-xs text-ink-muted">模型调用</div>
              <div class="text-lg font-semibold text-ink-strong">{{ run.totals.llm_calls }}</div>
            </div>
            <div class="rounded-lg bg-surface-muted p-3">
              <div class="text-xs text-ink-muted">耗时</div>
              <div class="text-lg font-semibold text-ink-strong">{{ (run.totals.latency_ms / 1000).toFixed(1) }} 秒</div>
            </div>
            <div class="rounded-lg bg-surface-muted p-3">
              <div class="text-xs text-ink-muted">成本</div>
              <div class="text-lg font-semibold text-ink-strong">¥{{ run.totals.cost_cny.toFixed(4) }}</div>
            </div>
            <div class="rounded-lg bg-surface-muted p-3">
              <div class="text-xs text-ink-muted">热点数</div>
              <div class="text-lg font-semibold text-ink-strong">{{ run.hotspots.length }}</div>
            </div>
          </div>
          <div>
            <n-button tag="a" :href="reportUrl" target="_blank" type="primary" size="small">
              打开完整报告（HTML）
            </n-button>
          </div>
        </div>
      </n-card>

      <n-card v-if="run" title="执行与证据">
        <div class="flex flex-col gap-2 text-sm text-ink">
          <p>
            模型：{{ snapshot.llm?.provider || '未知' }} /
            {{ snapshot.llm?.model || '未知' }}；
            topk：{{ snapshot.retrieval?.topk ?? '未知' }}
          </p>
          <div class="flex flex-wrap gap-2">
            <n-tag
              v-for="[name, meta] in Object.entries(run.evidence || {})"
              :key="name"
              size="small"
              :type="meta.completeness === 'complete' ? 'success'
                : meta.completeness === 'partial' ? 'warning' : 'error'"
            >
              {{ name }}：{{ EVIDENCE_ORIGIN_LABELS[meta.origin] }}
              （{{ meta.completeness }}）
            </n-tag>
          </div>
        </div>
      </n-card>

      <n-card v-if="run" title="数据可视化">
        <RunCharts :run="run" />
      </n-card>

      <!-- 四个结果块抽在 `HotspotResult.vue`（S7.5）：结果页与对话首页的结果卡片共用 -->
      <template v-for="(hotspot, idx) in run?.hotspots ?? []" :key="hotspot.hotspot_id">
        <n-card :title="`热点 ${idx + 1}`">
          <p class="mb-4 text-base font-medium text-ink-strong">
            {{ hotspot.hotspot_raw }}
          </p>
          <HotspotResult :hotspot="hotspot" :run-id="run?.run_id || runId"
                         :position="hotspot.position" />
        </n-card>
      </template>

      <n-card v-if="!run && loading" title="加载中…" />
    </div>
  </main>
</template>
