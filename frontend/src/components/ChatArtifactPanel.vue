<script setup lang="ts">
import { NAlert, NButton, NCard, NInput, NInputNumber, NModal, NSelect, NTag } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import type { components } from '@/api/schema'
import { ELEMENT_TYPE_LABELS, ELEMENT_TYPES } from '@/api/analysis'
import { useChatStore } from '@/stores/chat'

defineProps<{ disabled?: boolean }>()
type Clue = components['schemas']['HotspotClue']
const store = useChatStore()
const editing = ref(false)
const busy = ref(false)
const message = ref<string | null>(null)
const rawText = ref('')
const editor = ref<Clue | null>(null)
let epoch = 0
const preview = computed(() => store.artifactPreview)
const evidence = computed(() => (preview.value?.evidence_snapshot ?? {}) as {
  origin?: string
  completeness?: string
  missing_fields?: string[]
  current_material?: { exists?: boolean }
})
const evidenceNote = computed(() => {
  if (!preview.value || preview.value.object_type !== 'draft') return ''
  if (evidence.value.completeness === 'missing') {
    return `历史依据不完整（缺失：${(evidence.value.missing_fields ?? []).join('、') || '未知'}），不应用当前素材补写`
  }
  const unavailable = evidence.value.current_material?.exists === false
  const source = evidence.value.origin === 'runtime'
    ? '运行时保存' : evidence.value.origin === 'legacy_run_record'
      ? '原运行记录' : '历史采集'
  return `本次改稿使用${source}的文字依据${unavailable ? '；原素材当前不可用' : ''}`
})
const heads = computed(() => store.context?.artifact_heads ?? [])
const elementOptions = ELEMENT_TYPES.map(type => ({ label: ELEMENT_TYPE_LABELS[type], value: type }))
const draftBody = computed(() => String(preview.value?.content?.body ?? ''))
const draftTitles = computed(() => {
  const value = preview.value?.content?.titles
  return Array.isArray(value) ? value.map(item => (
    typeof item === 'object' && item !== null && 'text' in item ? String(item.text) : String(item)
  )) : []
})
const draftTags = computed(() => {
  const value = preview.value?.content?.tags
  return Array.isArray(value) ? value.map(String).join(' · ') : ''
})
const selectedClue = computed(() => store.context?.object_refs?.find(
  item => item.object_key === preview.value?.object_key,
)?.version_id === preview.value?.version_id && preview.value !== null)
const sourceOpen = computed({
  get: () => Boolean(store.sourcePreview || store.sourceError),
  set: (value: boolean) => { if (!value) { store.sourcePreview = null; store.sourceError = null } },
})

watch(() => store.currentId, () => { epoch += 1; editing.value = false; busy.value = false; message.value = null })
watch(() => preview.value?.version_id, () => { editing.value = false; message.value = null })

function objectLabel(key: string): string {
  return key.startsWith('draft:') ? `文案 · 热点 ${key.split(':').at(-1) ?? ''}` : '图片线索'
}

async function act(operation: () => Promise<string | null>): Promise<void> {
  const current = epoch
  busy.value = true
  message.value = null
  try {
    const result = await operation()
    if (epoch === current) message.value = result
  } finally {
    if (epoch === current) busy.value = false
  }
}

function chooseDraft(): void {
  const version = preview.value
  if (!version || version.object_type !== 'draft' || version.status !== 'succeeded') return
  store.selectedBase = { objectKey: version.object_key, versionId: version.version_id,
                         versionNo: version.version_no ?? 0 }
}

function editClue(): void {
  const content = preview.value?.content
  if (!content || typeof content.clue !== 'object' || content.clue === null) return
  rawText.value = String(content.raw_text ?? '')
  editor.value = JSON.parse(JSON.stringify(content.clue)) as Clue
  editing.value = true
}

function lines(value: string): string[] {
  return value.split('\n').map(item => item.trim()).filter(Boolean)
}

function addElement(): void {
  editor.value?.elements.push({ type: 'topic', value: '', weight: 0.6, confidence: 0.9, evidence: '' })
}

async function save(): Promise<void> {
  const clue = editor.value
  if (!clue || !rawText.value.trim() || !clue.elements.length
      || clue.elements.some(item => !item.value.trim())) {
    message.value = '请填写热点描述和至少一个有效要素'
    return
  }
  const content = { raw_text: rawText.value.trim(), clue: { ...clue, hotspot_raw: rawText.value.trim() },
    edited_fields: ['raw_text', 'why_it_works', 'mechanisms', 'elements',
                    'match_keywords', 'borrow_angles', 'risk_notes'] }
  await act(() => store.saveClue(content))
  if (message.value === null) editing.value = false
}
</script>

<template>
  <section v-if="heads.length" class="flex flex-col gap-3 border-t border-line pt-3" data-testid="artifact-panel">
    <span class="text-xs text-ink-muted">版本历史与线索</span>
    <div class="flex flex-wrap gap-2">
      <n-button v-for="head in heads" :key="head.object_key" size="tiny"
                :disabled="disabled || busy" @click="store.loadArtifacts(head.object_key)">
        {{ objectLabel(head.object_key) }} · 查看历史版本
      </n-button>
    </div>
    <n-alert v-if="store.artifactsError || message" type="warning" :title="store.artifactsError || message || ''" />
    <span v-if="store.artifactsLoading" class="text-xs text-ink-muted">正在读取版本…</span>
    <div v-if="store.artifactVersions?.length" class="flex max-h-36 flex-wrap gap-2 overflow-y-auto">
      <n-button v-for="version in store.artifactVersions" :key="version.version_id" size="tiny"
                :type="preview?.version_id === version.version_id ? 'primary' : 'default'"
                :disabled="disabled || busy" @click="store.previewArtifact(version.version_id)">
        {{ version.version_no ? `第 ${version.version_no} 版` : '未完成版本' }}
        {{ version.status === 'succeeded' ? '' : '· 未成功' }}
      </n-button>
    </div>
    <n-card v-if="preview" size="small" :title="`${objectLabel(preview.object_key)} · 第 ${preview.version_no ?? '—'} 版`">
      <div class="flex flex-col gap-3">
        <span class="text-xs text-ink-muted">{{ preview.created_at }} · 历史内容保持不变</span>
        <n-alert v-if="evidenceNote" type="info" :title="evidenceNote" />
        <n-tag v-if="selectedClue" size="tiny" type="success">当前选定线索</n-tag>
        <div v-if="preview.object_type === 'draft'" class="whitespace-pre-wrap text-sm text-ink-strong">
          <p v-for="title in draftTitles" :key="title" class="font-medium">{{ title }}</p>
          <p class="my-2">{{ draftBody }}</p>
          <p class="text-xs text-ink-muted">{{ draftTags }}</p>
        </div>
        <div v-else-if="!editing" class="whitespace-pre-wrap text-sm text-ink-strong">
          <p>{{ preview.content?.raw_text }}</p>
          <p class="mt-2 text-xs text-ink-muted">编辑后另存为新版本，重解析会保留已选定的编辑版。</p>
        </div>
        <div v-if="editing && editor" class="flex flex-col gap-3" data-testid="clue-editor">
          <label class="text-xs text-ink-muted">热点描述
            <n-input v-model:value="rawText" type="textarea" maxlength="500" show-count placeholder="热点描述" />
          </label>
          <label class="text-xs text-ink-muted">为什么有效（每行一条）
            <n-input :value="editor.why_it_works.join('\n')" type="textarea" placeholder="为什么有效"
                     @update:value="value => { if (editor) editor.why_it_works = lines(value) }" />
          </label>
          <div v-for="(element, index) in editor.elements" :key="index" class="flex flex-col gap-2 rounded border border-line p-2">
            <div class="flex flex-wrap items-center gap-2">
              <n-select v-model:value="element.type" :options="elementOptions" class="w-28" aria-label="要素类型" />
              <n-input v-model:value="element.value" placeholder="要素内容" class="min-w-32 flex-1" />
              <n-button size="tiny" @click="editor?.elements.splice(index, 1)">移除要素</n-button>
            </div>
            <div class="flex flex-wrap gap-3 text-xs text-ink-muted">
              <label>权重 <n-input-number v-model:value="element.weight" :min="0" :max="1" :step="0.1" class="w-28" /></label>
              <label>可信度 <n-input-number v-model:value="element.confidence" :min="0" :max="1" :step="0.1" class="w-28" /></label>
            </div>
            <n-input v-model:value="element.evidence" placeholder="要素依据" />
          </div>
          <n-button size="tiny" @click="addElement">添加要素</n-button>
          <div v-for="(mechanism, index) in editor.mechanisms" :key="index" class="flex gap-2">
            <n-input v-model:value="mechanism.name" placeholder="传播机制名称" />
            <n-input v-model:value="mechanism.explain" placeholder="机制说明" />
            <n-button size="tiny" @click="editor?.mechanisms.splice(index, 1)">移除</n-button>
          </div>
          <n-button size="tiny" @click="editor.mechanisms.push({ name: '', explain: '' })">添加机制</n-button>
          <label class="text-xs text-ink-muted">检索关键词（每行一个）
            <n-input :value="(editor.match_keywords ?? []).join('\n')" type="textarea" placeholder="检索关键词"
                     @update:value="value => { if (editor) editor.match_keywords = lines(value) }" />
          </label>
          <label class="text-xs text-ink-muted">借势角度（每行一条）
            <n-input :value="(editor.borrow_angles ?? []).join('\n')" type="textarea" placeholder="借势角度"
                     @update:value="value => { if (editor) editor.borrow_angles = lines(value) }" />
          </label>
          <label class="text-xs text-ink-muted">风险提示（每行一条）
            <n-input :value="(editor.risk_notes ?? []).join('\n')" type="textarea" placeholder="风险提示"
                     @update:value="value => { if (editor) editor.risk_notes = lines(value) }" />
          </label>
          <div class="flex gap-2">
            <n-button type="primary" :loading="busy" :disabled="disabled" @click="save">保存并选定新版</n-button>
            <n-button :disabled="busy" @click="editing = false">取消编辑</n-button>
          </div>
        </div>
        <div v-if="preview.status === 'succeeded'" class="flex flex-wrap gap-2">
          <n-button v-if="preview.object_type === 'draft'" size="tiny" :disabled="disabled || busy" @click="chooseDraft">
            以这版为基础改稿
          </n-button>
          <template v-else>
            <n-button size="tiny" :disabled="disabled || busy || editing" @click="editClue">编辑这版线索</n-button>
            <n-button size="tiny" :disabled="disabled || busy || editing || selectedClue"
                      @click="act(() => store.chooseClue(preview!.version_id))">选用这版线索</n-button>
            <n-button size="tiny" :disabled="disabled || busy || editing" @click="act(store.reparseClue)">重新解析原图</n-button>
          </template>
          <n-button v-if="typeof preview.source?.message_id === 'string' && preview.source.message_id" size="tiny"
                    @click="store.showSource(String(preview.source?.message_id))">查看来源消息</n-button>
        </div>
        <span v-if="store.selectedBase" class="text-xs text-ink-muted">
          下次改稿使用第 {{ store.selectedBase.versionNo }} 版，发送后清除本次选择。
        </span>
      </div>
    </n-card>
  </section>
  <n-modal v-model:show="sourceOpen" preset="card" title="来源消息" style="width: min(640px, 92vw)">
    <n-alert v-if="store.sourceError" type="warning" :title="store.sourceError" />
    <div v-if="store.sourcePreview" class="whitespace-pre-wrap text-sm">
      <p class="mb-3 text-xs text-ink-muted">{{ store.sourcePreview.created_at }}</p>
      <p>{{ store.sourcePreview.content }}</p>
      <img v-for="attachment in store.sourcePreview.attachments" :key="attachment.url" :src="attachment.url"
           :alt="attachment.name" class="mt-3 max-h-64 max-w-full rounded">
    </div>
  </n-modal>
</template>
