<script setup lang="ts">
import { NAlert, NButton, NCard, NEmpty, NProgress, NTag } from 'naive-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiRequestError, apiUrl } from '@/api/client'
import type { RunDetail } from '@/api/analysis'
import { ELEMENT_TYPE_LABELS, getKeyframeUrl, getRun } from '@/api/analysis'
import RunCharts from '@/components/RunCharts.vue'

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

/** 关键帧加载失败的素材（隐藏 img、展示占位），键为 material_id */
const frameFailed = reactive<Record<string, boolean>>({})

function onFrameError(materialId: string): void {
  frameFailed[materialId] = true
}

// ---------- 文案一键复制（每个热点独立状态，键为 hotspot_id） ----------
const copiedMap = reactive<Record<string, boolean>>({})
const copyTimers = new Map<string, number>()

type Draft = NonNullable<RunDetail['hotspots'][number]['draft']>

function draftToText(draft: Draft): string {
  const lines: string[] = []
  if (draft.titles.length > 0) {
    lines.push('【标题备选】')
    for (const t of draft.titles) {
      lines.push(`- ${t.text}${t.style ? `（${t.style}）` : ''}`)
    }
  }
  if (draft.cover_text) lines.push(`\n【封面字】${draft.cover_text}`)
  if (draft.first_3s) lines.push(`\n【开头 3 秒】${draft.first_3s}`)
  lines.push(`\n【正文】\n${draft.body}`)
  if (draft.tags.length > 0) lines.push(`\n【话题】${draft.tags.join(' ')}`)
  if (draft.shot_list && draft.shot_list.length > 0) {
    lines.push('\n【剪辑顺序】')
    for (const [i, shot] of draft.shot_list.entries()) {
      lines.push(`${i + 1}. ${shot}`)
    }
  }
  if (draft.compliance_notes && draft.compliance_notes.length > 0) {
    lines.push('\n【合规提醒】')
    for (const note of draft.compliance_notes) {
      lines.push(`- ${note}`)
    }
  }
  return lines.join('\n')
}

async function copyDraft(draft: Draft, hotspotId: string): Promise<void> {
  const text = draftToText(draft)
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // clipboard API 不可用（非安全上下文等）时走降级
    const textarea = document.createElement('textarea')
    textarea.value = text
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
  }
  copiedMap[hotspotId] = true
  const prev = copyTimers.get(hotspotId)
  if (prev !== undefined) window.clearTimeout(prev)
  copyTimers.set(hotspotId, window.setTimeout(() => {
    copiedMap[hotspotId] = false
  }, 2000))
}
</script>

<template>
  <main class="px-4 py-8">
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
        结果写回后即可查看（409）。可稍后刷新，或回到分析台重新提交。
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

      <n-card v-if="run" title="数据可视化">
        <RunCharts :run="run" />
      </n-card>

      <template v-for="(hotspot, idx) in run?.hotspots ?? []" :key="hotspot.hotspot_id">
        <n-card :title="`热点 ${idx + 1}`">
          <div class="flex flex-col gap-6">
            <p class="text-base font-medium text-ink-strong">
              {{ hotspot.hotspot_raw }}
            </p>

            <!-- 块一：爆点要素 -->
            <section>
              <h2 class="mb-2 text-sm font-semibold text-ink-strong">
                爆点要素
              </h2>
              <div class="flex flex-wrap gap-2">
                <n-tag v-for="(el, i) in hotspot.clue.elements" :key="i" size="small" :title="`权重 ${(el.weight * 100).toFixed(0)}%`">
                  {{ ELEMENT_TYPE_LABELS[el.type] }} · {{ el.value }}
                </n-tag>
                <span v-if="hotspot.clue.elements.length === 0" class="text-sm text-ink-muted">未识别到要素</span>
              </div>
              <ul v-if="hotspot.clue.why_it_works.length > 0" class="mt-2 list-disc pl-5 text-sm text-ink">
                <li v-for="(why, i) in hotspot.clue.why_it_works" :key="i">
                  {{ why }}
                </li>
              </ul>
              <div v-if="hotspot.clue.risk_notes && hotspot.clue.risk_notes.length > 0" class="mt-2">
                <n-alert type="warning" title="不可直用的要素（侵权/合规风险）">
                  <ul class="list-disc pl-5 text-sm">
                    <li v-for="(note, i) in hotspot.clue.risk_notes" :key="i">
                      {{ note }}
                    </li>
                  </ul>
                </n-alert>
              </div>
            </section>

            <!-- 块二：候选素材（含关键帧） -->
            <section>
              <h2 class="mb-2 text-sm font-semibold text-ink-strong">
                候选素材（{{ hotspot.candidates.length }}）
              </h2>
              <n-empty v-if="hotspot.candidates.length === 0" description="没有命中的素材" />
              <div v-for="cand in hotspot.candidates" :key="cand.material_id" class="flex gap-4 border-t border-line py-4 first:border-t-0">
                <div class="h-24 w-36 shrink-0 overflow-hidden rounded-lg bg-surface-muted">
                  <img
                    v-if="cand.material.keyframes?.length && !frameFailed[cand.material.id]"
                    :src="getKeyframeUrl(cand.material.id, 0)"
                    :alt="cand.material.title ?? '关键帧'"
                    class="h-full w-full object-cover"
                    @error="onFrameError(cand.material.id)"
                  >
                  <div v-else class="flex h-full w-full items-center justify-center text-xs text-ink-muted">
                    无预览
                  </div>
                </div>
                <div class="min-w-0 flex-1">
                  <div class="flex items-baseline justify-between gap-2">
                    <div class="font-medium text-ink-strong">
                      #{{ cand.rank }} {{ cand.material.title || cand.material.path.split(/[\\/]/).pop() }}
                    </div>
                    <div class="shrink-0 text-sm font-semibold" :class="cand.score >= 0.6 ? 'text-brand-strong' : 'text-ink'">
                      {{ (cand.score * 100).toFixed(0) }} 分
                    </div>
                  </div>
                  <div class="mt-1 flex flex-wrap items-center gap-1 text-xs text-ink-muted">
                    <n-tag v-for="src in cand.recall_sources" :key="src" size="tiny" :type="src === 'vector' ? 'info' : 'default'">
                      {{ src === 'vector' ? '语义召回' : '字面召回' }}
                    </n-tag>
                    <span v-if="cand.material.duration_s">{{ cand.material.duration_s.toFixed(1) }}s · </span>
                    <span>{{ cand.material.type === 'video' ? '视频' : '图片' }}</span>
                  </div>
                  <div v-if="cand.hits.length > 0" class="mt-2 flex flex-wrap gap-1">
                    <n-tag v-for="(hit, i) in cand.hits" :key="i" size="tiny" type="success">
                      {{ ELEMENT_TYPE_LABELS[hit.element_type] }} · {{ hit.clue_value }}
                    </n-tag>
                  </div>
                  <ul class="mt-2 list-disc pl-5 text-sm text-ink">
                    <li v-for="(reason, i) in cand.reasons" :key="i">
                      {{ reason }}
                    </li>
                  </ul>
                  <p class="mt-1 text-sm text-ink">
                    <span class="font-medium">建议用法：</span>{{ cand.usage }}
                  </p>
                </div>
              </div>
            </section>

            <!-- 块三：覆盖缺口 -->
            <section>
              <h2 class="mb-2 text-sm font-semibold text-ink-strong">
                覆盖缺口
              </h2>
              <div class="flex items-center gap-3">
                <n-progress type="line" :percentage="Math.round(hotspot.coverage.ratio * 100)" class="flex-1" />
                <span class="text-sm text-ink">要素覆盖率 {{ (hotspot.coverage.ratio * 100).toFixed(0) }}%</span>
              </div>
              <div v-if="hotspot.coverage.covered.length > 0" class="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <span class="text-ink-muted">已覆盖：</span>
                <n-tag v-for="(el, i) in hotspot.coverage.covered" :key="i" size="tiny" type="success">
                  {{ ELEMENT_TYPE_LABELS[el.type] }} · {{ el.value }}
                </n-tag>
              </div>
              <div class="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <span class="text-ink-muted">缺口：</span>
                <n-tag v-for="(el, i) in hotspot.coverage.gaps" :key="i" size="tiny" type="warning">
                  {{ ELEMENT_TYPE_LABELS[el.type] }} · {{ el.value }}
                </n-tag>
                <span v-if="hotspot.coverage.gaps.length === 0" class="text-sm text-ink-muted">无缺口，素材全要素覆盖</span>
              </div>
            </section>

            <!-- 块四：文案初稿（一键复制） -->
            <section v-if="hotspot.draft">
              <div class="mb-2 flex items-center justify-between">
                <h2 class="text-sm font-semibold text-ink-strong">
                  文案初稿
                </h2>
                <n-button size="small" :disabled="copiedMap[hotspot.hotspot_id]" @click="copyDraft(hotspot.draft, hotspot.hotspot_id)">
                  {{ copiedMap[hotspot.hotspot_id] ? '已复制 ✓' : '一键复制' }}
                </n-button>
              </div>
              <div v-if="hotspot.draft.titles.length > 0" class="mb-2 flex flex-wrap gap-2">
                <n-tag v-for="(t, i) in hotspot.draft.titles" :key="i" size="small" type="info">
                  {{ t.text }}<template v-if="t.style">（{{ t.style }}）</template>
                </n-tag>
              </div>
              <div class="rounded-lg border border-line bg-surface p-4 text-sm text-ink-strong">
                <p v-if="hotspot.draft.cover_text" class="mb-2">
                  <span class="font-medium">封面字：</span>{{ hotspot.draft.cover_text }}
                </p>
                <p v-if="hotspot.draft.first_3s" class="mb-2">
                  <span class="font-medium">开头 3 秒：</span>{{ hotspot.draft.first_3s }}
                </p>
                <p class="whitespace-pre-wrap">{{ hotspot.draft.body }}</p>
                <p v-if="hotspot.draft.tags.length > 0" class="mt-2 text-xs text-ink-muted">
                  {{ hotspot.draft.tags.join(' ') }}
                </p>
              </div>
              <ol v-if="hotspot.draft.shot_list && hotspot.draft.shot_list.length > 0" class="mt-2 list-decimal pl-5 text-sm text-ink">
                <li v-for="(shot, i) in hotspot.draft.shot_list" :key="i">
                  {{ shot }}
                </li>
              </ol>
              <ul v-if="hotspot.draft.compliance_notes && hotspot.draft.compliance_notes.length > 0" class="mt-2 list-disc pl-5 text-xs text-ink-muted">
                <li v-for="(note, i) in hotspot.draft.compliance_notes" :key="i">
                  {{ note }}
                </li>
              </ul>
            </section>
            <n-alert v-else type="info" title="本次未产出文案初稿" />
          </div>
        </n-card>
      </template>

      <n-card v-if="!run && loading" title="加载中…" />
    </div>
  </main>
</template>
