<script setup lang="ts">
// 对话首页（S7.5）：传图（最多 9 张，S7.18）+ 一句话 → 模型自己决定要不要调「跑完整分析」→ 过程流式可见 →
// 结果卡片回到对话里。刷新后会话与消息仍在（库里读回来），运行中的那轮靠轮询补齐。
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NCollapse,
  NCollapseItem,
  NEmpty,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import type { ChatMessage, ContextFact, ContextProposal } from '@/api/chat'
import HotspotResult from '@/components/HotspotResult.vue'
import ChatArtifactPanel from '@/components/ChatArtifactPanel.vue'
import ImageIntake from '@/components/ImageIntake.vue'
import StepTimeline from '@/components/StepTimeline.vue'
import { errorCategoryLabel, stageLabel, useChatStore } from '@/stores/chat'
import { formatElapsed } from '@/utils/format'

const store = useChatStore()
const message = useMessage()

const draft = ref('')
const picked = ref<File[]>([])
const messagesEl = ref<HTMLElement | null>(null)
const dropActive = ref(false)

/** composer 变体只暴露这几个方法给 ChatView；校验 / 命名 / object URL 仍在 ImageIntake 内。 */
interface IntakeExpose {
  pick: () => void
  addFiles: (files: File[], options?: { pasted?: boolean }) => void
  handlePaste: (event: ClipboardEvent) => void
  previews: string[]
}
const intake = ref<IntakeExpose | null>(null)
const previews = computed(() => intake.value?.previews ?? [])
const canInteract = computed(() => !store.busy && !store.creatingSession)
const canSend = computed(() =>
  (draft.value.trim().length > 0 || picked.value.length > 0) && canInteract.value)
const currentTitle = computed(() => store.currentSession?.title || '新对话')

/** 贴底判据：距底 40px 以内算「用户还在底部」，此时新消息/流式增量才自动跟随。 */
const STICK_THRESHOLD_PX = 40
const stickToBottom = ref(true)

function nearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX
}

function onMessagesScroll(): void {
  const el = messagesEl.value
  if (el !== null) stickToBottom.value = nearBottom(el)
}

/** 滚到最新一条；`force` 用于「打开会话 / 首次进入」这种必须贴底的场景。 */
async function scrollToLatest(force = false): Promise<void> {
  if (!force && !stickToBottom.value) return
  await nextTick()
  const el = messagesEl.value
  if (el === null) return
  el.scrollTop = el.scrollHeight
  stickToBottom.value = true
}

// 新消息 / 流式增量 / 工具步骤变化：只有贴底时才跟随——用户上翻读历史时不要拽回去。
// 工具步骤现在按阶段就地更新（长度不变），所以用「最后一行内容」而不是 length 当触发源。
watch(
  [
    () => store.messages.length,
    () => store.streamText,
    () => store.toolSteps.map((step) => `${step.stage}:${step.done}:${step.status}`).join('|'),
  ],
  () => {
    void scrollToLatest()
  },
)

// 切换（或首次打开）会话：直接跳到底，语义是「看最新的」
watch(() => store.currentId, () => {
  contextActionError.value = null
  contextBusy.value = false
  acknowledgeGap.value = false
  draft.value = ''
  picked.value = []
  void scrollToLatest(true)
})

onMounted(async () => {
  await store.init()
  await scrollToLatest(true)
})

// 消息里带 run_id 的结果卡片：拿到 id 就把 run 取回来（store 里缓存，失败只记 null）
watch(() => store.messages, (items) => {
  for (const item of items) {
    const runId = cardRunId(item)
    if (runId) void store.ensureRun(runId)
  }
}, { immediate: true })

/** 结果卡片只属于**产出**这次运行的那一轮。
 *
 * `read_run` / `revise_draft` 的返回里也带 `run_id`，后端会把它落到这条助手消息上（那是"本轮
 * 引用了哪次运行"，用于上下文与版本追溯）。若照 `run_id` 直接渲染卡片，用户每追问一句都会把
 * 同一份热点与候选清单再贴一遍——所以这里只认本轮真的调过 `run_hotspot_analysis` 的消息。
 */
function cardRunId(item: ChatMessage): string {
  const produced = (item.tool_calls ?? []).some((call) => call.tool === 'run_hotspot_analysis')
  return produced ? item.run_id ?? '' : ''
}

/** 校验失败只弹 toast；ImageIntake 成功时发出的空错误串必须忽略（不弹空 toast）。 */
function onIntakeError(text: string): void {
  if (text) message.warning(text, { duration: 4000 })
}

async function submit(): Promise<void> {
  if (!canSend.value) return
  const id = store.currentId
  const text = draft.value
  const files = picked.value
  draft.value = ''
  picked.value = []
  const result = await store.send({ text, files })
  const switched = id !== null && store.currentId !== id
  // 只有「确认未受理」与「前置校验未发出」恢复草稿；已受理后断流不恢复、不自动重发（D16）。
  if (!switched && (result.outcome === 'rejected' || result.outcome === 'invalid')) {
    draft.value = text
    picked.value = files
  }
  if (!switched && result.outcome !== 'accepted' && result.message) {
    message.warning(result.message, { duration: 4000 })
  }
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  void submit()
}

/** 无边框 textarea 随内容长到 6 行（约 144px）为止，之后自己滚。 */
const composerInput = ref<HTMLTextAreaElement | null>(null)
function autoGrow(): void {
  const el = composerInput.value
  if (el === null) return
  el.style.height = 'auto'
  el.style.height = `${String(Math.min(el.scrollHeight, 144))}px`
}
watch(draft, () => { void nextTick(autoGrow) })

function pickImages(): void {
  intake.value?.pick()
}

function removeImage(index: number): void {
  picked.value = picked.value.filter((_file, position) => position !== index)
}

function onComposerDragOver(event: DragEvent): void {
  event.preventDefault()
  dropActive.value = true
}

function onComposerDragLeave(event: DragEvent): void {
  const next = event.relatedTarget as Node | null
  if (next !== null && (event.currentTarget as HTMLElement).contains(next)) return
  dropActive.value = false
}

function onComposerDrop(event: DragEvent): void {
  event.preventDefault()
  dropActive.value = false
  intake.value?.addFiles(Array.from(event.dataTransfer?.files ?? []))
}

function onComposerPaste(event: ClipboardEvent): void {
  intake.value?.handlePaste(event)
}

function messageText(message: ChatMessage): string {
  if (message.status === 'running') return store.streamText
  return message.content
}

function statusLabel(message: ChatMessage): string | null {
  switch (message.status) {
    case 'running':
      return '正在回复…'
    case 'failed':
      return '这一轮失败了'
    case 'interrupted':
      return '这一轮没跑完就断了'
    default:
      return null
  }
}

/** 运行中的一行摘要：当前阶段（没有阶段时退回工具名、「正在回复…」）+ 已用时。
 *
 *  只给「在动」的信号，不给百分比——排队这类阶段的百分比本来就不存在。
 */
function runningSummary(message: ChatMessage): string {
  const label = store.progress?.label || store.activeTool?.label || statusLabel(message) || ''
  const parts: string[] = []
  if (label !== '') parts.push(label)
  if (store.stageElapsedMs > 0) parts.push(`已 ${formatElapsed(store.stageElapsedMs)}`)
  return parts.join(' · ')
}

// 阶段流水默认收起；换一轮（sending 由 false → true）时复位
const stepsExpanded = ref(false)
watch(() => store.sending, (on) => {
  if (on) stepsExpanded.value = false
})

/** 失败提示：有阶段快照时补「失败于哪个阶段 + 安全错误类别」（S7.21）。 */
function failureHint(message: ChatMessage): string {
  const base = message.error || statusLabel(message) || '这一轮没有结果'
  const snapshot = message.progress
  if (!snapshot || String(snapshot.status ?? '') !== 'failed') return base
  const parts = [`失败于「${stageLabel(String(snapshot.stage ?? ''))}」`]
  if (String(snapshot.error_category ?? '') !== '') {
    parts.push(errorCategoryLabel(String(snapshot.error_category)))
  }
  return `${base}（${parts.join(' · ')}）`
}

// ---- 上下文面板（S7.24）：生效 / 待确认、作用域、来源与恢复缺口 ----
const FACT_KIND_LABELS: Record<string, string> = {
  goal: '目标',
  constraint: '限制',
  preference: '偏好',
  audience: '受众',
  format: '形式',
}

const SCOPE_LABELS: Record<string, string> = {
  session: '整个会话',
  topic: '当前话题',
  artifact: '产物',
}

const PROPOSAL_REASON_LABELS: Record<string, string> = {
  inferred: '推断',
  conflict: '与现有要求冲突',
  ambiguous_reference: '指代不明确',
  scope_unresolved: '对象未确认',
}

const MODE_LABELS: Record<string, string> = {
  off: '未启用',
  shadow: '影子模式',
  enforce: '已生效',
}

const contextActionError = ref<string | null>(null)
const contextBusy = ref(false)
const acknowledgeGap = ref(false)
const recoveryLabel = computed(() => {
  const status = store.context?.recovery?.status
  return ({ pending: '待扫描', running: '正在扫描', awaiting_confirmation: '等待核对',
            failed: '本页失败', interrupted: '本页已中断', complete: '已完成' })[status ?? 'pending']
})

const facts = computed<ContextFact[]>(() => store.context?.facts ?? [])
const proposals = computed<ContextProposal[]>(() => store.context?.proposals ?? [])
const activeFacts = computed(() => facts.value.filter((item) => item.status === 'active'))
const pendingFacts = computed(
  () => facts.value.filter((item) => item.status === 'pending_confirmation'),
)
const contextSummary = computed(() => {
  const loaded = store.context
  if (loaded === null) return '尚未加载'
  return `生效 ${activeFacts.value.length} 条 · 待确认 ${proposals.value.length} 条`
})

function factKindLabel(kind: string): string {
  return FACT_KIND_LABELS[kind] ?? kind
}

function scopeLabel(fact: ContextFact): string {
  const base = SCOPE_LABELS[fact.scope] ?? fact.scope
  return fact.scope === 'session' || !fact.scope_id ? base : `${base}（${fact.scope_id}）`
}

function proposalReasonLabel(reason: string): string {
  return PROPOSAL_REASON_LABELS[reason] ?? reason
}

async function actOnContext(
  action: 'confirm' | 'reject',
  proposalId: string,
): Promise<void> {
  const id = store.currentId
  contextActionError.value = null
  contextBusy.value = true
  try {
    const error = await store.actOnContext(action, proposalId)
    if (store.currentId === id) contextActionError.value = error
  } finally {
    if (store.currentId === id) contextBusy.value = false
  }
}

async function onStartRecovery(): Promise<void> {
  const id = store.currentId
  contextActionError.value = null
  contextBusy.value = true
  try {
    const error = await store.startRecovery()
    if (store.currentId === id) contextActionError.value = error
  } finally {
    if (store.currentId === id) contextBusy.value = false
  }
}

async function onCompleteRecovery(): Promise<void> {
  const id = store.currentId
  contextActionError.value = null
  contextBusy.value = true
  try {
    const error = await store.completeRecovery(acknowledgeGap.value)
    if (store.currentId === id) contextActionError.value = error
  } finally {
    if (store.currentId === id) contextBusy.value = false
  }
}

function versionTag(call: { version_no?: number | null; base_version_no?: number | null }): string | null {
  if (call.version_no === null || call.version_no === undefined) return null
  const base = call.base_version_no !== null && call.base_version_no !== undefined
    ? `（基础 第 ${call.base_version_no} 版）`
    : ''
  return `第 ${call.version_no} 版${base}`
}
</script>

<template>
  <!-- 对话页自己锁高度：外壳给的是 min-h-0 flex-1，这里 h-full 撑满 → 全页只有消息区滚动 -->
  <main class="h-full min-h-0 px-4 py-6">
    <div class="mx-auto flex h-full min-h-0 w-full max-w-5xl flex-col gap-4">
      <header class="flex shrink-0 items-center gap-3">
        <h1 class="line-clamp-1 min-w-0 text-lg font-semibold text-ink-strong">
          {{ currentTitle }}
        </h1>
      </header>

      <n-alert v-if="store.error" class="shrink-0" type="error" :title="store.error" />

      <!-- 当前目标与限制（S7.24）：折叠面板；刷新、重连后与服务端一致，不被流式正文重置 -->
      <n-card v-if="store.currentId" size="small" class="max-h-[45vh] shrink-0 overflow-y-auto">
        <n-collapse :default-expanded-names="proposals.length > 0 ? ['context'] : []">
          <n-collapse-item name="context">
            <template #header>
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-medium text-ink-strong">当前目标与限制</span>
                <n-tag size="tiny" :bordered="false">
                  {{ contextSummary }}
                </n-tag>
                <n-tag
                  v-if="store.context"
                  size="tiny"
                  :type="store.context.effective_mode === 'enforce' ? 'success' : 'default'"
                  :bordered="false"
                >
                  {{ MODE_LABELS[store.context.effective_mode] ?? store.context.effective_mode }}
                </n-tag>
              </div>
            </template>

            <div class="flex flex-col gap-2 text-sm">
              <n-alert
                v-if="store.context?.recovery_status === 'partial'"
                type="warning"
                title="历史状态未完整恢复：更早的要求可能没读回来，请补充关键限制"
              />
              <n-alert v-if="store.contextError" type="warning" :title="store.contextError" />
              <n-alert v-if="contextActionError" type="warning" :title="contextActionError" />

              <div v-if="store.contextLoading" class="text-xs text-ink-muted">加载中…</div>

              <template v-else-if="activeFacts.length === 0 && proposals.length === 0">
                <span class="text-xs text-ink-muted">
                  还没有明确的目标或限制。直接说「控制在 100 字以内」「别提价格」这类要求即可。
                </span>
              </template>

              <ul v-if="activeFacts.length > 0" class="flex flex-col gap-1">
                <li
                  v-for="fact in activeFacts"
                  :key="fact.fact_id"
                  class="flex flex-wrap items-baseline gap-2"
                >
                  <n-tag size="tiny" type="success" :bordered="false">
                    {{ factKindLabel(fact.kind) }}
                  </n-tag>
                  <span class="text-ink-strong">{{ fact.value }}</span>
                  <span class="text-xs text-ink-muted">{{ scopeLabel(fact) }}</span>
                  <span v-if="fact.evidence" class="text-xs text-ink-muted">
                    · 原文「{{ fact.evidence }}」
                  </span>
                  <n-button v-if="typeof fact.source_ref?.message_id === 'string'" size="tiny" text
                            @click="store.showSource(String(fact.source_ref?.message_id))">查看来源</n-button>
                </li>
              </ul>

              <div v-if="proposals.length > 0" class="flex flex-col gap-2 border-t border-line pt-2">
                <span class="text-xs text-ink-muted">待确认（确认前不按它执行）</span>
                <div
                  v-for="proposal in proposals"
                  :key="proposal.proposal_id"
                  class="flex flex-wrap items-center gap-2"
                >
                  <n-tag size="tiny" type="warning" :bordered="false">
                    {{ factKindLabel(proposal.kind) }}
                  </n-tag>
                  <span class="text-ink-strong">{{ proposal.value }}</span>
                  <span class="text-xs text-ink-muted">
                    {{ proposalReasonLabel(proposal.reason) }}
                  </span>
                  <n-button v-if="typeof proposal.source_ref?.message_id === 'string'" size="tiny" text
                            @click="store.showSource(String(proposal.source_ref?.message_id))">查看来源</n-button>
                  <div class="ml-auto flex gap-2">
                    <n-button
                      size="tiny"
                      type="primary"
                      :disabled="contextBusy"
                      @click="actOnContext('confirm', proposal.proposal_id)"
                    >
                      确认
                    </n-button>
                    <n-button
                      size="tiny"
                      :disabled="contextBusy"
                      @click="actOnContext('reject', proposal.proposal_id)"
                    >
                      拒绝
                    </n-button>
                  </div>
                </div>
              </div>

              <span v-if="pendingFacts.length > 0" class="text-xs text-ink-muted">
                另有 {{ pendingFacts.length }} 条生效事实等待补全来源
              </span>

              <!-- 旧会话恢复（v2 §3.5）：显式触发、候选确认、缺口显式确认后完成 -->
              <div
                v-if="store.context && store.context.recovery
                  && store.context.recovery.status !== 'complete'"
                class="flex flex-col gap-2 border-t border-line pt-2"
              >
                <div class="flex flex-wrap items-center gap-2">
                  <n-tag size="tiny" type="warning" :bordered="false">
                    历史恢复：{{ recoveryLabel }}
                  </n-tag>
                  <span class="text-xs text-ink-muted">
                    已扫描 {{ store.context.recovery.scanned_user_messages }} /
                    {{ store.context.recovery.total_user_messages }} 条用户消息
                  </span>
                  <div class="ml-auto flex gap-2">
                    <n-button
                      size="tiny"
                      type="primary"
                      :disabled="contextBusy || store.context.recovery.status === 'running'
                        || store.context.recovery.status === 'awaiting_confirmation'"
                      @click="onStartRecovery"
                    >
                      {{ ['failed', 'interrupted'].includes(store.context.recovery.status)
                        ? '重试本页' : (store.context.recovery.recovery_id ? '扫描下一页' : '开始恢复') }}
                    </n-button>
                    <n-button
                      size="tiny"
                      :disabled="contextBusy || proposals.length > 0
                        || store.context.recovery.status === 'running'
                        || store.context.recovery.status === 'pending'"
                      @click="onCompleteRecovery"
                    >
                      已核对并完成
                    </n-button>
                  </div>
                </div>
                <n-button v-if="store.context.recovery.status === 'running'" size="tiny" @click="store.loadContext">
                  刷新恢复状态
                </n-button>
                <n-checkbox v-if="['failed', 'interrupted'].includes(store.context.recovery.status)
                  || (store.context.recovery.gaps?.length ?? 0) > 0"
                            v-model:checked="acknowledgeGap">
                  我已补全当前要求，理解未扫描的历史仍有缺口
                </n-checkbox>
                <span class="text-xs text-ink-muted">
                  恢复完成前不会执行分析 / 改稿；候选全部待确认，确认后才生效。
                </span>
              </div>

              <ChatArtifactPanel :disabled="contextBusy || store.busy" />
            </div>
          </n-collapse-item>
        </n-collapse>
      </n-card>

      <section class="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
          <!-- 唯一滚动容器：贴底判据与自动跟随都挂在它身上 -->
          <div
            ref="messagesEl"
            class="chat-messages flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1"
            @scroll.passive="onMessagesScroll"
          >
            <n-empty
              v-if="store.messages.length === 0 && !store.busy"
              description="还没有消息，说一句话或丢张图试试"
              class="py-10"
            >
              <template #extra>
                <span class="text-sm text-ink-muted">
                  例：「帮我蹭一下这张图里的热点」+ 一张热点截图
                </span>
              </template>
            </n-empty>

            <div v-for="message in store.messages" :key="message.message_id" class="flex flex-col gap-2">
              <div
                class="max-w-full rounded-2xl px-4 py-3 text-sm"
                :class="message.role === 'user'
                  ? 'ml-auto bg-brand text-white'
                  : 'mr-auto border border-line bg-surface'"
              >
                <p v-if="messageText(message)" class="whitespace-pre-wrap text-ink-strong">
                  {{ messageText(message) }}
                </p>
                <div v-if="message.attachments.length > 0" class="mt-2 flex flex-wrap gap-2">
                  <img
                    v-for="(attachment, index) in message.attachments"
                    :key="attachment.url"
                    :src="attachment.url"
                    :alt="attachment.name || `附件 ${index + 1}`"
                    class="h-20 w-20 rounded-lg object-cover"
                  >
                </div>

                <!-- 工具过程：一行摘要 + 需要时才展开的阶段时间线（默认收起） -->
                <div v-if="message.status === 'running'" class="mt-2 flex flex-col gap-1 text-xs text-ink-muted">
                  <div class="flex flex-wrap items-center gap-2 border-t border-line pt-2">
                    <n-spin size="small" />
                    <span>{{ runningSummary(message) }}</span>
                    <span v-if="(store.progress?.attempt ?? 1) > 1">
                      第 {{ store.progress?.attempt }} 次尝试
                    </span>
                    <button
                      v-if="store.toolSteps.length > 0"
                      type="button"
                      class="ml-auto underline underline-offset-2"
                      :aria-expanded="stepsExpanded"
                      @click="stepsExpanded = !stepsExpanded"
                    >
                      {{ stepsExpanded ? '收起' : '详情' }}
                    </button>
                  </div>
                  <StepTimeline v-if="stepsExpanded" :steps="store.toolSteps" />
                  <span v-if="store.stageLongRunning">
                    耗时较长：可能还在排队或素材同步，稍等即可，不必刷新页面。
                  </span>
                </div>
                <div v-else-if="message.status !== 'succeeded'" class="mt-2 flex flex-col gap-1">
                  <n-alert type="warning" :title="failureHint(message)" />
                </div>
              </div>

              <!-- 持久化的工具过程（刷新后回看） -->
              <div v-if="message.tool_calls.length > 0" class="mr-auto flex flex-wrap gap-1">
                <n-tag
                  v-for="call in message.tool_calls"
                  :key="`${call.call_id ?? call.tool}-${call.run_id ?? ''}`"
                  size="tiny"
                  :type="call.status === 'succeeded' ? 'success' : 'warning'"
                >
                  {{ store.toolLabel(call.tool) }} ·
                  {{ call.status === 'succeeded' ? '已跑完' : '失败了' }}
                  <template v-if="versionTag(call)"> · {{ versionTag(call) }}</template>
                </n-tag>
              </div>

              <!-- 调用链回看（S7.21）：只有有效 trace 关联时才显示；追踪关闭时整行不出现 -->
              <a
                v-if="message.trace_url"
                class="mr-auto text-xs text-ink-muted underline"
                :href="message.trace_url"
                target="_blank"
                rel="noreferrer"
              >
                查看调用链
              </a>

              <!-- 结果卡片：和结果页共用 `HotspotResult` -->
              <template v-if="cardRunId(message) && store.runs[cardRunId(message)]">
                <n-card
                  v-for="(hotspot, index) in store.runs[cardRunId(message)]?.hotspots ?? []"
                  :key="hotspot.hotspot_id"
                  :title="`热点 ${index + 1}`"
                  size="small"
                >
                  <p class="mb-3 text-sm font-medium text-ink-strong">
                    {{ hotspot.hotspot_raw }}
                  </p>
                  <HotspotResult :hotspot="hotspot" compact
                                 :run-id="cardRunId(message)" :position="hotspot.position" />
                </n-card>
                <n-alert
                  v-if="(store.runs[cardRunId(message)]?.hotspots.length ?? 0) === 0"
                  :type="store.runs[cardRunId(message)]?.status === 'failed' ? 'error' : 'info'"
                  :title="store.runs[cardRunId(message)]?.status === 'failed'
                    ? '这次运行没有产出结果' : '这次运行没有热点结果'"
                >
                  {{ store.runs[cardRunId(message)]?.hotspots[0]?.error || '可到运行历史查看详情。' }}
                </n-alert>
              </template>
              <div
                v-else-if="cardRunId(message) && cardRunId(message) in store.runs"
                class="mr-auto text-xs text-ink-muted"
              >
                结果暂不可用（run_id：{{ cardRunId(message) }}），可稍后刷新
              </div>
              <div
                v-else-if="cardRunId(message)"
                class="mr-auto text-xs text-ink-muted"
              >
                结果卡片加载中（run_id：{{ cardRunId(message) }}）
              </div>
            </div>
          </div>

          <!-- 整块 composer：缩略图 / 无边框输入 / ＋ · 张数 · 圆形发送 -->
          <div
            class="chat-composer shrink-0"
            :class="{ 'is-dragging': dropActive }"
            @dragover="onComposerDragOver"
            @dragleave="onComposerDragLeave"
            @drop="onComposerDrop"
            @paste="onComposerPaste"
          >
            <div v-if="dropActive" class="composer-drop-mask">松手添加图片</div>

            <div v-if="picked.length > 0" class="mb-2 flex flex-wrap gap-2">
              <div v-for="(file, index) in picked" :key="`${file.name}-${index}`" class="composer-thumb">
                <img
                  v-if="previews[index]"
                  :src="previews[index]"
                  :alt="file.name"
                  class="h-full w-full rounded-[10px] object-cover"
                >
                <button
                  type="button"
                  class="composer-thumb-remove"
                  :aria-label="`移除 ${file.name}`"
                  @click="removeImage(index)"
                >
                  ×
                </button>
              </div>
            </div>

            <textarea
              ref="composerInput"
              v-model="draft"
              class="composer-input"
              rows="1"
              placeholder="想蹭哪条热点？说一句话，或者直接丢图"
              :disabled="store.busy"
              @input="autoGrow"
              @keydown="onComposerKeydown"
            />

            <div class="mt-2 flex items-center gap-2">
              <button
                type="button"
                class="composer-add"
                title="图片只用于本次解析、不落素材库"
                aria-label="添加图片"
                :disabled="!canInteract"
                @click="pickImages"
              >
                ＋
              </button>
              <span class="text-xs text-ink-muted">最多 9 张</span>
              <span class="text-xs text-ink-muted">已选 {{ picked.length }} / 9 张</span>
              <button
                type="button"
                class="composer-send ml-auto"
                :disabled="!canSend"
                aria-label="发送"
                @click="submit"
              >
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                  <path
                    d="M12 19V5M5 12l7-7 7 7"
                    fill="none" stroke="currentColor" stroke-width="2"
                    stroke-linecap="round" stroke-linejoin="round"
                  />
                </svg>
                <span class="sr-only">发送</span>
              </button>
            </div>

            <!-- 选择 / 拖拽 / 粘贴的校验仍只在 ImageIntake 一处；这里只留隐藏 input -->
            <ImageIntake
              ref="intake"
              v-model:files="picked"
              variant="composer"
              :max="9"
              :disabled="!canInteract"
              @error="onIntakeError"
            />
          </div>
      </section>
    </div>
  </main>
</template>
