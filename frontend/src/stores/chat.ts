import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import { ApiRequestError } from '@/api/client'
import type { ChatMessage, ChatSessionSummary } from '@/api/chat'
import { createChatSession, getChatSession, listChatSessions, streamMessage } from '@/api/chat'

// 一次工具调用的进度步骤（S7.4 的 tool_started / tool_progress / tool_finished 归约而成）。
export interface ToolStep {
  stage: string
  done: number
  total: number
}

const POLL_INTERVAL_MS = 2000
const SESSION_PAGE_SIZE = 20

// 工具名的中文标签（工具 schema 的单一来源是后端 services/chat_tools.py::TOOLS；
// 这里只做展示，没登记的名字原样回落到英文名，不会挡住新工具上线）。
export const TOOL_LABELS: Record<string, string> = {
  run_hotspot_analysis: '跑完整分析',
  parse_image_clue: '解析图片',
  search_materials: '查素材库',
  read_run: '读历史运行',
  preview_element_coverage: '预览爆点要素',
  revise_draft: '改写文案',
}

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name
}

// 阶段键的中文标签（后端 `core/progress.py::STAGE_LABELS` 的展示映射；未登记的原样显示）
export const STAGE_LABELS: Record<string, string> = {
  supervisor: '理解与决策',
  image_parse: '解析图片',
  submit_analysis: '提交分析',
  queue_wait: '排队等待',
  material_sync: '素材同步',
  embedding_backfill: '向量回填',
  embedding_query: '向量化查询',
  clue: '拆解',
  retrieve: '检索',
  gaps: '缺口',
  draft: '撰稿',
  report: '报告',
  final_reply: '整理回复',
}

export function stageLabel(key: string): string {
  return STAGE_LABELS[key] ?? key
}

// 安全错误类别 → 中文（后端只给类别、不给错误正文；S7.19 白名单口径）
export const ERROR_CATEGORY_LABELS: Record<string, string> = {
  llm_error: '模型调用失败',
  embedding_error: '向量化失败',
  schema_error: '输出结构不合法',
  config_error: '配置错误',
  timeout: '超时',
  cancelled: '已取消',
  dependency_unavailable: '依赖不可用',
  internal: '内部错误',
}

export function errorCategoryLabel(key: string): string {
  return ERROR_CATEGORY_LABELS[key] ?? key
}

/** 当前阶段视图（S7.21 / E7OBS4-04）：来自 SSE 事件里的**权威快照**或刷新后的
 *  `message.progress`；两者是同一份后端快照，前端只做投影，不自行拼装状态。 */
export interface StageView {
  stage: string
  label: string
  status: string
  attempt: number
  startedAt: string
  errorCategory: string
  /** 当前阶段所属执行（worker run_id）；对话侧阶段为空串。R5：身份隔离用。 */
  runId: string
  sourceId: string
  stageInstanceId: string
  sourceSeq: number
  /** 后端已接受快照的语义版本（E7OBS4-04）：只在同一消息 / 轮次内可比。 */
  progressRevision: number
}

/** revision 的比较范围（E7OBS4-04）：不同消息 / 不同轮次的版本号不可比。 */
interface ProgressScope {
  messageId: string
  turnId: string
}

/** 跑起来超过这个时长就提示「耗时较长」——只是提示，不据此断定失败。 */
export const LONG_STAGE_MS = 60_000

/** 阶段终态（R3 单调合并用）：同一次执行的终态不因重复事件重开。 */
export const TERMINAL_STAGE_STATUSES = ['succeeded', 'failed', 'skipped', 'interrupted']

/** 把事件里的 `progress` 字段读成快照对象；不是对象 / 没有阶段键就当没带（旧格式）。 */
function asSnapshot(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  return String(raw.stage ?? '') === '' ? null : raw
}

// 对话首页的状态：会话列表 / 当前会话的消息 / 本轮流式增量与工具步骤 / 命中过的结果卡片。
export const useChatStore = defineStore('chat', () => {
  const sessions = ref<ChatSessionSummary[]>([])
  const sessionsLoading = ref(false)
  const sessionsError = ref<string | null>(null)

  const currentId = ref<string | null>(null)
  const messages = ref<ChatMessage[]>([])
  const messagesLoading = ref(false)

  const sending = ref(false)
  const streamText = ref('')
  const toolSteps = ref<ToolStep[]>([])
  const error = ref<string | null>(null)

  // 当前阶段（S7.21）：SSE 实时更新 + 刷新后从 message.progress 恢复；已耗时每秒刷新
  const progress = ref<StageView | null>(null)
  // E7OBS4-04：revision 的比较范围——切会话 / 新消息 / 新轮次都重置，不跨范围比较
  const progressScope = ref<ProgressScope>({ messageId: '', turnId: '' })
  const clock = ref(Date.now())
  let clockTimer: number | undefined

  function startClock(): void {
    if (clockTimer === undefined) {
      clockTimer = window.setInterval(() => {
        clock.value = Date.now()
      }, 1000)
    }
  }

  function stopClock(): void {
    if (clockTimer !== undefined) {
      window.clearInterval(clockTimer)
      clockTimer = undefined
    }
  }

  const stageElapsedMs = computed(() => {
    const item = progress.value
    if (item === null || item.status !== 'running') return 0
    const started = Date.parse(item.startedAt)
    if (Number.isNaN(started)) return 0
    return Math.max(0, clock.value - started)
  })
  const stageLongRunning = computed(
    () => progress.value?.status === 'running' && stageElapsedMs.value >= LONG_STAGE_MS,
  )

  // run_id → RunDetail（结果卡片）；null 表示「取不到」（还没写回 / 已被清理）
  const runs = ref<Record<string, RunDetail | null>>({})

  let pollTimer: number | undefined

  const currentSession = computed(
    () => sessions.value.find((item) => item.session_id === currentId.value) ?? null,
  )
  const busy = computed(() => sending.value)

  function describeError(err: unknown): string {
    if (err instanceof ApiRequestError) return err.message
    if (err instanceof Error) return err.message
    return '未知错误'
  }

  function hasRunning(items: ChatMessage[]): boolean {
    return items.some((item) => item.status === 'running')
  }

  function sameScope(scope: ProgressScope): boolean {
    return progressScope.value.messageId === scope.messageId
      && progressScope.value.turnId === scope.turnId
  }

  /** 把一份后端快照读成阶段视图（字段名是后端的 snake_case）。
   *
   *  起点**只认快照**：同一阶段实例沿用旧起点，换实例 / 换阶段时若快照没带 `started_at`
   *  才用当前时刻兜底——绝不因为"又收到一条事件"就重置已耗时。
   */
  function viewOf(
    raw: Record<string, unknown>,
    previous: StageView | null,
  ): StageView {
    const stage = String(raw.stage ?? '')
    const instance = String(raw.stage_instance_id ?? '')
    const runId = String(raw.run_id ?? '')
    const sourceId = String(raw.source_id ?? '')
    const startedAt = String(raw.started_at ?? '')
    const sameInstance = previous !== null
      && previous.stage === stage
      && previous.stageInstanceId === instance
    return {
      stage,
      label: String(raw.label ?? '') || stageLabel(stage),
      status: String(raw.status ?? 'running'),
      attempt: Number(raw.attempt ?? 1),
      startedAt: startedAt !== ''
        ? startedAt
        : (sameInstance ? previous.startedAt : new Date().toISOString()),
      errorCategory: String(raw.error_category ?? ''),
      runId: runId !== '' ? runId : (sameInstance ? previous.runId : ''),
      sourceId: sourceId !== '' ? sourceId : (sameInstance ? previous.sourceId : ''),
      stageInstanceId: instance !== ''
        ? instance
        : (sameInstance ? previous.stageInstanceId : ''),
      sourceSeq: Number(raw.source_seq ?? 0),
      progressRevision: Number(raw.progress_revision ?? 0),
    }
  }

  function applyView(next: StageView): void {
    progress.value = next
    if (next.status === 'running') startClock()
    else stopClock()
  }

  /** 应用一份**后端已裁决**的权威快照（E7OBS4-04）。
   *
   *  - 只比较同一 `(message_id, turn_id)` 范围内的 revision；不同消息 / 轮次不可比，
   *    换范围时直接以新快照为准（旧范围的 revision 再小也不拦截）；
   *  - 更旧 revision 忽略；相同 revision 且阶段 / 实例 / 状态 / 尝试都一致视为幂等；
   *  - 较新 revision 是**完整投影**：允许换 run、换实例、较低 attempt——旧的
   *    「不同 run 终态一律拒绝」「同阶段同 attempt 终态不改」不再挡它（那只属于旧格式合并）。
   */
  function applySnapshot(raw: Record<string, unknown>, scope: ProgressScope): void {
    const previous = progress.value
    const inScope = previous !== null && sameScope(scope)
    const revision = Number(raw.progress_revision ?? 0)
    if (previous !== null && inScope) {
      const known = previous.progressRevision
      if (known > 0 && revision > 0 && revision < known) return
      if (known > 0 && revision > 0 && revision === known
        && previous.stage === String(raw.stage ?? '')
        && previous.status === String(raw.status ?? 'running')
        && previous.stageInstanceId === String(raw.stage_instance_id ?? '')
        && previous.attempt === Number(raw.attempt ?? 1)) return
    }
    progressScope.value = scope
    applyView(viewOf(raw, inScope ? previous : null))
  }

  /** 旧格式兼容合并（后端还没带 `progress` 的事件，或旧消息快照）。
   *
   *  R3（OBS-12）单调合并，与后端 `core/progress.py` 的旧口径一致：
   *  - 同一阶段、`attempt` 更小：旧尝试的迟到事件，忽略（不回退）；
   *  - 同一阶段、同一 `attempt` 且已是终态：不重开。
   *  R5（E7OBS2-02）身份隔离：两侧都有明确 `run_id` 且不一致 = 另一条执行，
   *  **终态一律不接管当前状态**；`running` 视为新执行开始（接替显示）。
   *  E7OBS4-04：一旦当前视图来自新格式权威快照（revision > 0），无 revision 的旧事件
   *  一律不能覆盖它。
   */
  function mergeLegacyStage(
    raw: Record<string, unknown> | null | undefined,
    scope?: ProgressScope,
  ): void {
    if (raw === null || raw === undefined) return
    const stage = String(raw.stage ?? '')
    if (stage === '') return
    if (progress.value !== null && progress.value.progressRevision > 0) return
    const attempt = Number(raw.attempt ?? 1)
    const status = String(raw.status ?? 'running')
    const incomingRun = String(raw.run_id ?? '')
    const incomingRevision = Number(raw.progress_revision ?? 0)
    const previous = progress.value
    if (previous !== null && incomingRevision > 0
      && previous.progressRevision > 0
      && incomingRevision < previous.progressRevision) return
    if (previous !== null && incomingRevision > 0
      && incomingRevision === previous.progressRevision
      && stage !== previous.stage) return
    const conflict = previous !== null && incomingRun !== ''
      && previous.runId !== '' && incomingRun !== previous.runId
    if (previous !== null && conflict && TERMINAL_STAGE_STATUSES.includes(status)) return
    if (previous !== null && !conflict && previous.stage === stage) {
      if (attempt < previous.attempt) return
      if (attempt === previous.attempt && TERMINAL_STAGE_STATUSES.includes(previous.status)) return
    }
    const sameStage = previous !== null && !conflict
      && previous.stage === stage && previous.attempt === attempt
    const startedAt = String(raw.started_at ?? '') || (sameStage ? previous.startedAt : '')
    if (scope !== undefined) progressScope.value = scope
    applyView({
      stage,
      label: String(raw.label ?? '') || stageLabel(stage),
      status,
      attempt,
      // 事件里可能只有 updated_at（如 tool_progress）：同阶段沿用旧起点，新阶段从此刻起算
      startedAt: startedAt !== '' ? startedAt : new Date().toISOString(),
      errorCategory: String(raw.error_category ?? ''),
      // 身份与当前阶段一致：带就用；同阶段缺身份保留已知身份；跨阶段缺身份清空（不串）
      runId: incomingRun !== '' ? incomingRun
        : (previous !== null && previous.stage === stage && !conflict ? previous.runId : ''),
      sourceId: String(raw.source_id ?? '') || (sameStage ? previous?.sourceId ?? '' : ''),
      stageInstanceId: String(raw.stage_instance_id ?? '')
        || (sameStage ? previous?.stageInstanceId ?? '' : ''),
      sourceSeq: Number(raw.source_seq ?? 0),
      progressRevision: incomingRevision > 0
        ? incomingRevision
        : (previous?.progressRevision ?? 0),
    })
  }

  /** 刷新 / 轮询后：从最近一条带快照的 assistant 消息恢复阶段；没有就清空。 */
  function syncProgressFromMessages(items: ChatMessage[]): void {
    const latest = [...items].reverse().find(
      (item) => item.role === 'assistant'
        && item.progress !== null && item.progress !== undefined
        && String(item.progress.stage ?? '') !== '',
    )
    if (latest?.progress) {
      // 刷新走**同一条权威投影**：不再重复执行一次原始阶段合并（E7OBS4-04）
      applySnapshot(latest.progress as unknown as Record<string, unknown>, {
        messageId: latest.message_id,
        turnId: latest.turn_id ?? '',
      })
    } else if (!hasRunning(items)) {
      progress.value = null
      progressScope.value = { messageId: '', turnId: '' }
      stopClock()
    }
  }

  function stopPolling(): void {
    if (pollTimer !== undefined) {
      window.clearInterval(pollTimer)
      pollTimer = undefined
    }
  }

  // 刷新后补齐：assistant 行还是 running 时按 2 秒轮询，落终态即停。
  function startPolling(): void {
    stopPolling()
    pollTimer = window.setInterval(() => {
      void pollOnce()
    }, POLL_INTERVAL_MS)
  }

  async function pollOnce(): Promise<void> {
    const sessionId = currentId.value
    if (sessionId === null) {
      stopPolling()
      return
    }
    try {
      const detail = await getChatSession(sessionId)
      // E7OBS4-04：旧请求返回前验证范围——切会话后迟到的响应不能覆盖新会话
      if (currentId.value !== sessionId) return
      messages.value = detail.messages
      syncProgressFromMessages(detail.messages)
      if (!hasRunning(detail.messages)) stopPolling()
    } catch {
      stopPolling()
    }
  }

  async function loadSessions(): Promise<void> {
    sessionsLoading.value = true
    sessionsError.value = null
    try {
      const listing = await listChatSessions({ limit: SESSION_PAGE_SIZE, offset: 0 })
      sessions.value = listing.items
    } catch (err) {
      sessionsError.value = describeError(err)
    } finally {
      sessionsLoading.value = false
    }
  }

  async function newSession(): Promise<string | null> {
    stopPolling()
    try {
      const created = await createChatSession()
      currentId.value = created.session_id
      messages.value = []
      streamText.value = ''
      toolSteps.value = []
      progress.value = null
      progressScope.value = { messageId: '', turnId: '' }
      error.value = null
      await loadSessions()
      return null
    } catch (err) {
      error.value = describeError(err)
      return error.value
    }
  }

  // 进页面时：拉会话列表；如果最近有一条会话就顺手打开它——刷新后消息仍在（S7.5 的验收口径）。
  async function init(): Promise<void> {
    await loadSessions()
    const latest = sessions.value[0]
    if (currentId.value === null && latest !== undefined) {
      await openSession(latest.session_id)
    }
  }

  async function openSession(sessionId: string): Promise<void> {
    stopPolling()
    currentId.value = sessionId
    messagesLoading.value = true
    error.value = null
    streamText.value = ''
    toolSteps.value = []
    progress.value = null
    progressScope.value = { messageId: '', turnId: '' }
    try {
      const detail = await getChatSession(sessionId)
      if (currentId.value !== sessionId) return   // 期间切了会话：旧响应不覆盖新视图
      messages.value = detail.messages
      syncProgressFromMessages(detail.messages)
      if (hasRunning(detail.messages)) startPolling()
    } catch (err) {
      messages.value = []
      error.value = describeError(err)
    } finally {
      messagesLoading.value = false
    }
  }

  async function refreshMessages(): Promise<void> {
    const sessionId = currentId.value
    if (sessionId === null) return
    try {
      const detail = await getChatSession(sessionId)
      if (currentId.value !== sessionId) return   // 旧会话响应不覆盖新会话
      messages.value = detail.messages
      syncProgressFromMessages(detail.messages)
    } catch {
      // 刷新失败不打断这一轮：流式增量仍在屏幕上，终态再对齐一次
    }
  }

  function onStreamEvent(event: string, data: Record<string, unknown>): void {
    // 事件信封带 message_id / turn_id（E7OBS4-04）：revision 只在这个范围内可比
    const scope: ProgressScope = {
      messageId: String(data.message_id ?? ''),
      turnId: String(data.turn_id ?? ''),
    }
    const snapshot = asSnapshot(data.progress)
    switch (event) {
      case 'turn_started':
        // 新轮次：范围先重置，避免拿上一轮的 revision 拦这一轮
        if (scope.turnId !== '') progressScope.value = scope
        void refreshMessages()
        break
      case 'stage':
        // 阶段生命周期（理解与决策 / 整理回复）：事件已带**合并后的权威快照**时只投影它；
        // 旧后端（没有 progress）才退回原始阶段字段合并。
        if (snapshot !== null) applySnapshot(snapshot, scope)
        else mergeLegacyStage(data, scope)
        break
      case 'tool_started':
        toolSteps.value.push({ stage: `开始：${toolLabel(String(data.tool ?? ''))}`, done: 0, total: 0 })
        break
      case 'tool_progress':
        // 工具过程（done/total 与中文阶段）只喂步骤列表，不再独立改写当前阶段
        toolSteps.value.push({
          stage: String(data.stage ?? ''),
          done: Number(data.done ?? 0),
          total: Number(data.total ?? 0),
        })
        if (snapshot !== null && String(data.stage_key ?? '') !== '') {
          applySnapshot(snapshot, scope)
        } else if (String(data.stage_key ?? '') !== '') {
          // 旧后端：结构化字段原样转发，真实状态 / 尝试号 / 来源身份不能被压成 running
          mergeLegacyStage({
            stage: String(data.stage_key ?? ''),
            label: String(data.stage ?? '') || stageLabel(String(data.stage_key)),
            status: String(data.status ?? 'running'),
            attempt: data.attempt,
            error_category: String(data.error_category ?? ''),
            run_id: String(data.run_id ?? ''),
            source_id: String(data.source_id ?? ''),
            stage_instance_id: String(data.stage_instance_id ?? ''),
            source_seq: data.source_seq,
            updated_at: data.updated_at,
          }, scope)
        }
        break
      case 'tool_finished':
        toolSteps.value.push({
          stage: data.status === 'succeeded' ? '工具完成' : `工具失败：${String(data.error ?? '')}`,
          done: 0,
          total: 0,
        })
        // 需要更新阶段的收尾事件同样带快照（结构化 worker 阶段 / 旧式标签阶段）
        if (snapshot !== null) applySnapshot(snapshot, scope)
        break
      case 'text_delta':
        streamText.value += String(data.text ?? '')
        break
      case 'turn_finished':
        if (snapshot !== null) applySnapshot(snapshot, scope)
        break
      case 'error':
        error.value = String(data.message ?? '这一轮失败了')
        // 失败 / 取消终态也带权威快照：终止计时并显示真实失败阶段
        if (snapshot !== null) applySnapshot(snapshot, scope)
        break
      default:
        break
    }
  }

  // 发一条消息（文字与图片至少给一个）。返回 null 表示已受理，否则是要给用户看的提示。
  async function send(input: { text: string; files?: File[] | null }): Promise<string | null> {
    const text = input.text.trim()
    const files = input.files ?? []
    if (text.length === 0 && files.length === 0) return '请先说点什么，或者丢图进来'
    if (sending.value) return '上一轮还在跑，等它说完再发'

    if (currentId.value === null) {
      const invalid = await newSession()
      if (invalid !== null) return invalid
    }
    const sessionId = currentId.value
    if (sessionId === null) return '会话没能建起来，请重试'

    sending.value = true
    error.value = null
    streamText.value = ''
    toolSteps.value = []
    progress.value = null
    progressScope.value = { messageId: '', turnId: '' }
    try {
      await streamMessage(sessionId, { text, files }, (event) => {
        onStreamEvent(event.event, event.data)
      })
    } catch (err) {
      error.value = describeError(err)
    } finally {
      sending.value = false
      streamText.value = ''
      await refreshMessages()
      await loadSessions()
      // 这一轮如果产出了 run，顺手把结果卡片的数据取回来
      const latest = [...messages.value].reverse().find((item) => item.run_id)
      if (latest?.run_id) void ensureRun(latest.run_id)
    }
    return null
  }

  // 取一次结果卡片的数据并缓存；409（还没写回）折成 null，不重复请求。
  async function ensureRun(runId: string): Promise<RunDetail | null> {
    const cached = runs.value[runId]
    if (cached !== undefined) return cached
    try {
      const detail = await getRun(runId)
      runs.value[runId] = detail
      return detail
    } catch {
      runs.value[runId] = null
      return null
    }
  }

  function reset(): void {
    stopPolling()
    stopClock()
    currentId.value = null
    messages.value = []
    streamText.value = ''
    toolSteps.value = []
    progress.value = null
    progressScope.value = { messageId: '', turnId: '' }
    error.value = null
  }

  return {
    sessions, sessionsLoading, sessionsError,
    currentId, currentSession, messages, messagesLoading,
    sending, busy, streamText, toolSteps, error, runs,
    progress, stageElapsedMs, stageLongRunning,
    loadSessions, init, newSession, openSession, refreshMessages, send, ensureRun, reset,
    stopPolling, toolLabel,
  }
})
