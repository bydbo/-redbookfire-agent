<script setup lang="ts">
import { NAlert, NButton, NCard, NEmpty, NPagination, NSpin, NTag, NTooltip } from 'naive-ui'
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import type { RunSummary } from '@/api/analysis'
import { listRuns } from '@/api/analysis'
import { apiUrl } from '@/api/client'

/** 每页条数：与契约默认一致（GET /api/runs 的 limit 上限 100）。 */
const PAGE_SIZE = 20

const router = useRouter()

const rows = ref<RunSummary[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const failure = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  failure.value = null
  try {
    const data = await listRuns({ limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE })
    rows.value = data.items
    total.value = data.total
  } catch (err) {
    failure.value = err instanceof Error ? err.message : '未知错误'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
})

watch(page, () => {
  void load()
})

type TagType = 'success' | 'error' | 'warning' | 'info' | 'default'

/** 状态标签（口径同分析台 / 结果页）。 */
function statusMeta(status: RunSummary['status']): { type: TagType; label: string } {
  switch (status) {
    case 'succeeded':
      return { type: 'success', label: '已完成' }
    case 'failed':
      return { type: 'error', label: '失败' }
    case 'running':
      return { type: 'info', label: '分析中' }
    default:
      return { type: 'warning', label: '排队中' }
  }
}

/** queued / running 取详情必然 409；failed 有部分结果，仍可回看（数据契约 §3.3）。 */
function isOpenable(row: RunSummary): boolean {
  return row.status === 'succeeded' || row.status === 'failed'
}

function shortId(runId: string): string {
  return runId.slice(0, 8)
}

function reportUrl(runId: string, format: 'html' | 'md'): string {
  return apiUrl(`/runs/${encodeURIComponent(runId)}/report?format=${format}`)
}

function openRun(runId: string): void {
  void router.push({ name: 'run-result', params: { runId } })
}
</script>

<template>
  <main class="px-4 py-8">
    <div class="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <header class="flex items-start justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">
            运行历史
          </h1>
          <p class="mt-1 text-sm text-ink">
            按创建时间倒序，共 {{ total }} 条；可回看结果或下载报告。
          </p>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <n-button size="small" :loading="loading" @click="load">
            刷新
          </n-button>
        </div>
      </header>

      <n-alert v-if="failure" type="error" :title="failure" />

      <n-card title="历史运行">
        <n-spin :show="loading">
          <n-empty v-if="!loading && rows.length === 0" description="还没有运行记录" />

          <div v-else class="flex flex-col divide-y divide-line">
            <div
              v-for="row in rows"
              :key="row.run_id"
              class="flex flex-col gap-3 py-4 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:gap-4"
            >
              <div class="flex w-40 shrink-0 items-center gap-2">
                <n-tag :type="statusMeta(row.status).type" size="small">
                  {{ statusMeta(row.status).label }}
                </n-tag>
                <span class="text-xs text-ink-muted" :title="row.run_id">#{{ shortId(row.run_id) }}</span>
              </div>

              <div class="min-w-0 flex-1">
                <div class="truncate text-sm text-ink-strong" :title="row.hotspot_preview">
                  {{ row.hotspot_preview || '（无热点）' }}
                </div>
                <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-muted">
                  <span>{{ new Date(row.created_at).toLocaleString() }}</span>
                  <span>{{ row.hotspot_count }} 个热点</span>
                  <span>topk {{ row.topk }}</span>
                  <span>成本 ¥{{ row.totals.cost_cny.toFixed(4) }}</span>
                  <span>耗时 {{ (row.totals.latency_ms / 1000).toFixed(1) }}s</span>
                  <span>{{ row.totals.llm_calls }} 次调用</span>
                </div>
                <p v-if="row.error" class="mt-1 truncate text-xs text-brand-strong" :title="row.error">
                  {{ row.error }}
                </p>
              </div>

              <div class="flex shrink-0 items-center gap-2">
                <n-button
                  v-if="isOpenable(row)"
                  size="small"
                  type="primary"
                  @click="openRun(row.run_id)"
                >
                  查看结果
                </n-button>
                <n-tooltip v-else>
                  <template #trigger>
                    <n-button size="small" disabled>
                      查看结果
                    </n-button>
                  </template>
                  运行尚未完成（{{ statusMeta(row.status).label }}），详情接口会返回 409
                </n-tooltip>

                <template v-if="isOpenable(row)">
                  <n-button
                    size="tiny"
                    tag="a"
                    :href="reportUrl(row.run_id, 'html')"
                    :download="`report-${row.run_id}.html`"
                  >
                    报告 HTML
                  </n-button>
                  <n-button
                    size="tiny"
                    tag="a"
                    :href="reportUrl(row.run_id, 'md')"
                    :download="`report-${row.run_id}.md`"
                  >
                    报告 MD
                  </n-button>
                </template>
              </div>
            </div>
          </div>
        </n-spin>

        <div v-if="total > PAGE_SIZE" class="mt-4 flex justify-center">
          <n-pagination
            v-model:page="page"
            :page-size="PAGE_SIZE"
            :item-count="total"
            :disabled="loading"
          />
        </div>
      </n-card>
    </div>
  </main>
</template>
