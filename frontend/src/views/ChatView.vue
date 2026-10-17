<script setup lang="ts">
// 对话首页（S7.5）：传图（最多 9 张，S7.18）+ 一句话 → 模型自己决定要不要调「跑完整分析」→ 过程流式可见 →
// 结果卡片回到对话里。刷新后会话与消息仍在（库里读回来），运行中的那轮靠轮询补齐。
import {
  NAlert,
  NButton,
  NCard,
  NCollapse,
  NCollapseItem,
  NEmpty,
  NInput,
  NSpin,
  NTag,
} from 'naive-ui'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import type { ChatMessage, ContextFact, ContextProposal } from '@/api/chat'
import HotspotResult from '@/components/HotspotResult.vue'
import ImageIntake from '@/components/ImageIntake.vue'
import { errorCategoryLabel, stageLabel, useChatStore } from '@/stores/chat'

const store = useChatStore()

const draft = ref('')
const picked = ref<File[]>([])
const intakeError = ref<string | null>(null)
const messagesEl = ref<HTMLElement | null>(null)

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

// 新消息 / 流式增量 / 工具步骤变化：只有贴底时才跟随——用户上翻读历史时不要拽回去
watch(
  [() => store.messages.length, () => store.streamText, () => store.toolSteps.length],
  () => {
    void scrollToLatest()
  },
)

// 切换（或首次打开）会话：直接跳到底，语义是「看最新的」
watch(() => store.currentId, () => {
  void scrollToLatest(true)
})

onMounted(async () => {
  await store.init()
  await scrollToLatest(true)
})

// 消息里带 run_id 的结果卡片：拿到 id 就把 run 取回来（store 里缓存，失败只记 null）
watch(() => store.messages, (items) => {
  for (const item of items) {
    if (item.run_id) void store.ensureRun(item.run_id)
  }
}, { immediate: true })

/** 图片入口的校验提示（白名单 / 单张 / 张数 / 合计，S7.17）；空串表示清掉 */
function onIntakeError(message: string): void {
  intakeError.value = message || null
}

async function onRemoveSession(): Promise<void> {
  contextActionError.value = await store.removeSession()
}

async function submit(): Promise<void> {
  const text = draft.value
  const file = picked.value
  draft.value = ''
  picked.value = []
  const invalid = await store.send({ text, files: file })
  if (invalid !== null) {
    draft.value = text
    picked.value = file
  }
  intakeError.value = null
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

function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

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

function sessionLabel(item: { title: string; session_id: string }): string {
  return item.title || '新对话'
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
  contextActionError.value = null
  contextBusy.value = true
  try {
    contextActionError.value = await store.actOnContext(action, proposalId)
  } finally {
    contextBusy.value = false
  }
}

function isSelected(head: { object_key: string; version_id: string }): boolean {
  return store.selectedBase?.objectKey === head.object_key
    && store.selectedBase.versionId === head.version_id
}

function selectBase(head: { object_key: string; version_id: string; version_no: number }): void {
  store.selectedBase = {
    objectKey: head.object_key,
    versionId: head.version_id,
    versionNo: head.version_no,
  }
}

async function onStartRecovery(): Promise<void> {
  contextActionError.value = null
  contextBusy.value = true
  try {
    contextActionError.value = await store.startRecovery()
  } finally {
    contextBusy.value = false
  }
}

async function onCompleteRecovery(): Promise<void> {
  contextActionError.value = null
  contextBusy.value = true
  try {
    const message = await store.completeRecovery(false)
    if (message !== null && message.includes('核对')) {
      // 有历史缺口：用户确认后携带显式确认重试
      contextActionError.value = await store.completeRecovery(true)
    } else {
      contextActionError.value = message
    }
  } finally {
    contextBusy.value = false
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
      <header class="flex shrink-0 flex-wrap items-end justify-between gap-3">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">
            热点搭子
          </h1>
          <p class="mt-1 text-sm text-ink">
            丢一张图或一句话过来，我帮你把热点拆开、找素材、写初稿（会自己决定要不要跑完整分析）。
          </p>
        </div>
        <div class="flex gap-2">
          <n-button size="small" :disabled="store.busy" @click="store.newSession()">
            新对话
          </n-button>
          <n-button
            v-if="store.currentId"
            size="small"
            :disabled="store.busy"
            @click="onRemoveSession"
          >
            删除会话
          </n-button>
        </div>
      </header>

      <n-alert v-if="store.error" class="shrink-0" type="error" :title="store.error" />

      <!-- 当前目标与限制（S7.24）：折叠面板；刷新、重连后与服务端一致，不被流式正文重置 -->
      <n-card v-if="store.currentId" size="small" class="shrink-0">
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
                    历史恢复：{{ store.context.recovery.status }}
                  </n-tag>
                  <span class="text-xs text-ink-muted">
                    已扫描 {{ store.context.recovery.scanned_user_messages }} /
                    {{ store.context.recovery.total_user_messages }} 条用户消息
                  </span>
                  <div class="ml-auto flex gap-2">
                    <n-button
                      size="tiny"
                      type="primary"
                      :disabled="contextBusy"
                      @click="onStartRecovery"
                    >
                      {{ store.context.recovery.status === 'pending' ? '开始恢复' : '继续恢复下一页' }}
                    </n-button>
                    <n-button
                      size="tiny"
                      :disabled="contextBusy"
                      @click="onCompleteRecovery"
                    >
                      已核对并完成
                    </n-button>
                  </div>
                </div>
                <span class="text-xs text-ink-muted">
                  恢复完成前不会执行分析 / 改稿；候选全部待确认，确认后才生效。
                </span>
              </div>

              <!-- 版本选择（v2 §3.3B）：选择只绑定下一次改稿，不改变 head -->
              <div
                v-if="store.context && store.context.artifact_heads.length > 0"
                class="flex flex-col gap-2 border-t border-line pt-2"
              >
                <span class="text-xs text-ink-muted">文案版本（选择只影响下一次改稿）</span>
                <div
                  v-for="head in store.context.artifact_heads"
                  :key="head.object_key"
                  class="flex flex-wrap items-center gap-2"
                >
                  <span class="text-xs text-ink-strong">{{ head.object_key }}</span>
                  <n-tag size="tiny" :bordered="false">第 {{ head.version_no }} 版</n-tag>
                  <n-button
                    v-if="head.object_key.startsWith('draft:')"
                    size="tiny"
                    :type="isSelected(head) ? 'primary' : 'default'"
                    :disabled="contextBusy"
                    @click="selectBase(head)"
                  >
                    {{ isSelected(head) ? '已选为下次基础' : '以此为基础改稿' }}
                  </n-button>
                </div>
                <span v-if="store.selectedBase" class="text-xs text-ink-muted">
                  下一次改稿将以第 {{ store.selectedBase.versionNo }} 版为基础
                  （发送后自动清除绑定）
                </span>
              </div>
            </div>
          </n-collapse-item>
        </n-collapse>
      </n-card>

      <div class="flex min-h-0 flex-1 flex-col gap-4 md:flex-row md:gap-6">
        <!-- 历史会话侧栏：消息区之外，自己滚；窄屏限高，别把消息区挤没 -->
        <aside class="w-full shrink-0 md:w-56">
          <n-card title="历史会话" size="small">
            <div class="chat-sessions-scroll max-h-28 overflow-y-auto md:max-h-80">
              <div v-if="store.sessionsLoading" class="text-sm text-ink-muted">
                加载中…
              </div>
              <n-alert v-else-if="store.sessionsError" type="warning" :title="store.sessionsError" />
              <n-empty v-else-if="store.sessions.length === 0" description="还没有会话" />
              <ul v-else class="flex flex-col gap-1">
                <li v-for="item in store.sessions" :key="item.session_id">
                  <button
                    type="button"
                    class="chat-session-item w-full rounded-lg px-2 py-1 text-left text-sm"
                    :class="{ 'is-active': item.session_id === store.currentId }"
                    @click="store.openSession(item.session_id)"
                  >
                    <span class="line-clamp-1 font-medium">{{ sessionLabel(item) }}</span>
                    <span class="mt-0.5 line-clamp-1 block text-xs text-ink-muted">
                      {{ item.message_count }} 条 · {{ item.last_message_preview }}
                    </span>
                  </button>
                </li>
              </ul>
            </div>
          </n-card>
        </aside>

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

                <!-- 工具过程：本轮流式中的粗粒度进度 -->
                <div
                  v-if="message.status === 'running' && store.toolSteps.length > 0"
                  class="mt-2 flex flex-col gap-1 border-t border-line pt-2 text-xs text-ink-muted"
                >
                  <span v-for="(step, index) in store.toolSteps" :key="`${index}-${step.stage}`">
                    · {{ step.stage }}<template v-if="step.total > 0">（{{ step.done }}/{{ step.total }}）</template>
                  </span>
                </div>

                <div v-if="message.status === 'running'" class="mt-2 flex flex-col gap-1 text-xs text-ink-muted">
                  <div class="flex flex-wrap items-center gap-2">
                    <n-spin size="small" />
                    <span>{{ statusLabel(message) }}</span>
                    <template v-if="store.progress">
                      <span>
                        · {{ store.progress.label }}
                        <template v-if="store.progress.attempt > 1">
                          （第 {{ store.progress.attempt }} 次尝试）
                        </template>
                      </span>
                      <span v-if="store.stageElapsedMs > 0">· 已 {{ formatElapsed(store.stageElapsedMs) }}</span>
                    </template>
                  </div>
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
              <template v-if="message.run_id && store.runs[message.run_id]">
                <n-card
                  v-for="(hotspot, index) in store.runs[message.run_id]?.hotspots ?? []"
                  :key="hotspot.hotspot_id"
                  :title="`热点 ${index + 1}`"
                  size="small"
                >
                  <p class="mb-3 text-sm font-medium text-ink-strong">
                    {{ hotspot.hotspot_raw }}
                  </p>
                  <HotspotResult :hotspot="hotspot" compact />
                </n-card>
              </template>
              <div
                v-else-if="message.run_id"
                class="mr-auto text-xs text-ink-muted"
              >
                结果卡片加载中（run_id：{{ message.run_id }}）
              </div>
            </div>
          </div>

          <!-- 输入区：一句话 +（可选）图（最多 9 张，S7.18） -->
          <n-card size="small" class="shrink-0">
            <div class="flex flex-col gap-3">
              <n-input
                v-model:value="draft"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 6 }"
                placeholder="想蹭哪条热点？说一句话就行（也可以只丢图，最多 9 张）"
                :disabled="store.busy"
                @keydown.enter.exact.prevent="submit"
              />
              <!-- 选择 / 拖拽 / 粘贴（P0）都在这个组件里；最多 9 张，超限整批拒绝 -->
              <ImageIntake
                v-model:files="picked"
                :max="9"
                :disabled="store.busy"
                title="丢几张图，合成一条热点"
                hint="点这里选文件、把图片拖进来，或先点一下这里再 Ctrl/⌘+V 粘贴（jpg / png / webp，单张 ≤ 10 MB，最多 9 张、合计 ≤ 30 MB）"
                label="选图片（最多 9 张）"
                @error="onIntakeError"
              />
              <n-alert v-if="intakeError" type="warning" :title="intakeError" />
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-xs text-ink-muted">
                  已选 {{ picked.length }} / 9 张
                </span>
                <n-button
                  class="ml-auto"
                  size="small"
                  type="primary"
                  :loading="store.busy"
                  :disabled="store.busy"
                  @click="submit"
                >
                  发送
                </n-button>
              </div>
              <p class="text-xs text-ink-muted">
                图片只用于这一次解析（不落盘到素材库）；离开页面这一轮也会跑完，回来看历史就行。
              </p>
            </div>
          </n-card>
        </section>
      </div>
    </div>
  </main>
</template>
