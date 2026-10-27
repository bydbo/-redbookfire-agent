<script setup lang="ts">
/**
 * 单个热点的结果四块（S7.5 从结果页抽出的纯展示组件）。
 *
 * 块一「爆点要素」→ 块二「候选素材（含关键帧）」→ 块三「覆盖缺口」→ 块四「文案初稿」，
 * 结果页（`RunResultView.vue`）与对话首页的结果卡片共用这一份，避免两处渲染各写一遍再漂移。
 *
 * 只有展示与「一键复制」，没有请求、没有路由——数据全由 props 进来。
 * 图表（`RunCharts`）是**运行级**的，仍只留在结果页，不进这个组件。
 */
import { NAlert, NButton, NProgress, NTag } from 'naive-ui'
import { reactive } from 'vue'

import type { HotspotResult } from '@/api/analysis'
import { ELEMENT_TYPE_LABELS, getRunKeyframeUrl } from '@/api/analysis'

const props = withDefaults(defineProps<{
  hotspot: HotspotResult
  /** 历史预览必须带运行与 1-based 热点位置，不能按当前素材 ID 取图 */
  runId: string
  position: number
  /** 对话里更紧凑：标题小一档、候选卡片不留大间距 */
  compact?: boolean
}>(), { compact: false })

/** 关键帧加载失败：键为 position:rank；候选可能没有素材摘要 */
const frameFailed = reactive<Record<string, boolean>>({})

function frameKey(rank: number): string {
  return `${props.position}:${rank}`
}

function canPreview(cand: HotspotResult['candidates'][number]): boolean {
  return Boolean(cand.material?.keyframes?.length
    && cand.current_material.preview_available
    && !frameFailed[frameKey(cand.rank)])
}

function previewUrl(cand: HotspotResult['candidates'][number]): string {
  return getRunKeyframeUrl(props.runId, props.position, cand.rank, 0)
}

function onFrameError(rank: number): void {
  frameFailed[frameKey(rank)] = true
}

function materialTitle(cand: HotspotResult['candidates'][number]): string {
  if (!cand.material) return '历史素材摘要缺失'
  return cand.material.title || cand.material.path.split(/[\\/]/).pop() || '未命名素材'
}

function currentStatus(cand: HotspotResult['candidates'][number]): string {
  if (!cand.current_material.exists) return '原素材已删除，历史文字依据仍保留'
  if (cand.current_material.reason === 'material_version_changed') {
    return '素材已更新，历史预览无法确认'
  }
  if (cand.current_material.reason === 'keyframes_missing') return '当前关键帧缺失'
  if (cand.current_material.reason === 'preview_unverified') return '历史预览缺少内容摘要，无法确认'
  if (cand.current_material.reason === 'keyframe_content_changed') return '关键帧已变化，历史预览无法确认'
  if (cand.current_material.file_available === false) return '原媒体文件缺失，关键帧可用性单独校验'
  if (!cand.current_material.preview_available) return '历史预览不可用'
  return ''
}

function emptyCandidatesNote(): string {
  const integrity = props.hotspot.evidence?.candidate_integrity
  if (integrity?.completeness === 'complete') return '没有命中的素材'
  if (integrity) return '旧候选完整性无法确认，不能当作“没有候选”'
  if (props.hotspot.status === 'failed') return '本次检索未完成或失败，尚无可确认候选'
  if (props.hotspot.status === 'running' || props.hotspot.status === 'queued') {
    return '本次检索尚未完成'
  }
  return '旧候选完整性无法确认，不能当作“没有候选”'
}

// ---------- 文案一键复制（键为 hotspot_id，同页多个热点互不干扰） ----------
const copiedMap = reactive<Record<string, boolean>>({})
const copyTimers = new Map<string, number>()

type Draft = NonNullable<HotspotResult['draft']>

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
  <div class="flex flex-col" :class="props.compact ? 'gap-4' : 'gap-6'">
    <!-- 块一：爆点要素 -->
    <section>
      <h2 class="mb-2 text-sm font-semibold text-ink-strong">
        爆点要素
      </h2>
      <n-alert
        v-if="!props.hotspot.clue"
        type="warning"
        title="历史线索缺失或无法确认，未用当前线索补历史"
      />
      <div class="flex flex-wrap gap-2">
        <n-tag
          v-for="(el, i) in props.hotspot.clue?.elements ?? []"
          :key="i"
          size="small"
          :title="`权重 ${(el.weight * 100).toFixed(0)}%`"
        >
          {{ ELEMENT_TYPE_LABELS[el.type] }} · {{ el.value }}
        </n-tag>
        <span v-if="(props.hotspot.clue?.elements.length ?? 0) === 0" class="text-sm text-ink-muted">未识别到要素</span>
      </div>
      <ul v-if="(props.hotspot.clue?.why_it_works.length ?? 0) > 0" class="mt-2 list-disc pl-5 text-sm text-ink">
        <li v-for="(why, i) in props.hotspot.clue?.why_it_works ?? []" :key="i">
          {{ why }}
        </li>
      </ul>
      <div v-if="(props.hotspot.clue?.risk_notes?.length ?? 0) > 0" class="mt-2">
        <n-alert type="warning" title="不可直用的要素（侵权/合规风险）">
          <ul class="list-disc pl-5 text-sm">
            <li v-for="(note, i) in props.hotspot.clue?.risk_notes ?? []" :key="i">
              {{ note }}
            </li>
          </ul>
        </n-alert>
      </div>
    </section>

    <!-- 块二：候选素材（含关键帧） -->
    <section>
      <h2 class="mb-2 text-sm font-semibold text-ink-strong">
        候选素材（{{ props.hotspot.candidates.length }}）
      </h2>
      <n-alert v-if="props.hotspot.status === 'failed'" type="error"
               :title="props.hotspot.error || '该热点执行失败，已完成依据会保留'" />
      <n-alert v-if="props.hotspot.candidates.length === 0"
               type="info"
               :title="emptyCandidatesNote()" />
      <div
        v-for="cand in props.hotspot.candidates"
        :key="cand.material_id"
        class="flex gap-4 border-t border-line py-4 first:border-t-0"
      >
        <div class="h-24 w-36 shrink-0 overflow-hidden rounded-lg bg-surface-muted">
          <img
            v-if="canPreview(cand)"
            :src="previewUrl(cand)"
            :alt="materialTitle(cand)"
            class="h-full w-full object-cover"
            @error="onFrameError(cand.rank)"
          >
          <div v-else class="flex h-full w-full items-center justify-center text-xs text-ink-muted">
            无预览
          </div>
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-baseline justify-between gap-2">
            <div class="font-medium text-ink-strong">
              #{{ cand.rank }} {{ materialTitle(cand) }}
            </div>
            <div class="shrink-0 text-sm font-semibold" :class="cand.score >= 0.6 ? 'text-brand-strong' : 'text-ink'">
              {{ (cand.score * 100).toFixed(0) }} 分
            </div>
          </div>
          <div class="mt-1 flex flex-wrap items-center gap-1 text-xs text-ink-muted">
            <n-tag v-for="src in cand.recall_sources" :key="src" size="tiny" :type="src === 'vector' ? 'info' : 'default'">
              {{ src === 'vector' ? '语义召回' : '字面召回' }}
            </n-tag>
            <span v-if="cand.material?.duration_s">{{ cand.material.duration_s.toFixed(1) }}s · </span>
            <span v-if="cand.material">{{ cand.material.type === 'video' ? '视频' : '图片' }}</span>
            <n-tag v-if="currentStatus(cand)" size="tiny" type="warning">
              {{ currentStatus(cand) }}
            </n-tag>
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
      <n-alert v-if="!props.hotspot.coverage" type="info"
               :title="props.hotspot.status === 'failed' ? '覆盖情况未知，本次检索未完成或失败' : '覆盖情况未知，尚无可确认的覆盖结果'" />
      <div v-else class="flex items-center gap-3">
        <n-progress type="line" :percentage="Math.round(props.hotspot.coverage.ratio * 100)" class="flex-1" />
        <span class="text-sm text-ink">要素覆盖率 {{ (props.hotspot.coverage.ratio * 100).toFixed(0) }}%</span>
      </div>
      <div v-if="props.hotspot.coverage && props.hotspot.coverage.covered.length > 0" class="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <span class="text-ink-muted">已覆盖：</span>
        <n-tag v-for="(el, i) in props.hotspot.coverage.covered" :key="i" size="tiny" type="success">
          {{ ELEMENT_TYPE_LABELS[el.type] }} · {{ el.value }}
        </n-tag>
      </div>
      <div v-if="props.hotspot.coverage" class="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <span class="text-ink-muted">缺口：</span>
        <n-tag v-for="(el, i) in props.hotspot.coverage.gaps" :key="i" size="tiny" type="warning">
          {{ ELEMENT_TYPE_LABELS[el.type] }} · {{ el.value }}
        </n-tag>
        <span v-if="props.hotspot.coverage.gaps.length === 0" class="text-sm text-ink-muted">无缺口，素材全要素覆盖</span>
      </div>
    </section>

    <!-- 块四：文案初稿（一键复制） -->
    <section v-if="props.hotspot.draft">
      <div class="mb-2 flex items-center justify-between">
        <h2 class="text-sm font-semibold text-ink-strong">
          文案初稿
        </h2>
        <n-button
          size="small"
          :disabled="copiedMap[props.hotspot.hotspot_id]"
          @click="copyDraft(props.hotspot.draft, props.hotspot.hotspot_id)"
        >
          {{ copiedMap[props.hotspot.hotspot_id] ? '已复制 ✓' : '一键复制' }}
        </n-button>
      </div>
      <div v-if="props.hotspot.draft.titles.length > 0" class="mb-2 flex flex-wrap gap-2">
        <n-tag v-for="(t, i) in props.hotspot.draft.titles" :key="i" size="small" type="info">
          {{ t.text }}<template v-if="t.style">（{{ t.style }}）</template>
        </n-tag>
      </div>
      <div class="rounded-lg border border-line bg-surface p-4 text-sm text-ink-strong">
        <p v-if="props.hotspot.draft.cover_text" class="mb-2">
          <span class="font-medium">封面字：</span>{{ props.hotspot.draft.cover_text }}
        </p>
        <p v-if="props.hotspot.draft.first_3s" class="mb-2">
          <span class="font-medium">开头 3 秒：</span>{{ props.hotspot.draft.first_3s }}
        </p>
        <p class="whitespace-pre-wrap">{{ props.hotspot.draft.body }}</p>
        <p v-if="props.hotspot.draft.tags.length > 0" class="mt-2 text-xs text-ink-muted">
          {{ props.hotspot.draft.tags.join(' ') }}
        </p>
      </div>
      <ol v-if="props.hotspot.draft.shot_list && props.hotspot.draft.shot_list.length > 0" class="mt-2 list-decimal pl-5 text-sm text-ink">
        <li v-for="(shot, i) in props.hotspot.draft.shot_list" :key="i">
          {{ shot }}
        </li>
      </ol>
      <ul v-if="props.hotspot.draft.compliance_notes && props.hotspot.draft.compliance_notes.length > 0" class="mt-2 list-disc pl-5 text-xs text-ink-muted">
        <li v-for="(note, i) in props.hotspot.draft.compliance_notes" :key="i">
          {{ note }}
        </li>
      </ul>
    </section>
    <n-alert v-else type="info" title="本次未产出文案初稿" />
  </div>
</template>
