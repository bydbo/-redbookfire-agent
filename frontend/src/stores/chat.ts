import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import { ApiRequestError } from '@/api/client'
import type {
  ChatContext,
  ChatMessage,
  ChatSessionSummary,
  ContextActionRequest,
  ArtifactVersion,
  ChatStreamInput,
} from '@/api/chat'
import {
  createChatSession,
  createClueVersion,
  deleteChatSession,
  getChatContext,
  getChatArtifact,
  getChatSession,
  listChatSessions,
  listChatArtifacts,
  postChatContextAction,
  renameChatSession,
  selectClueVersion,
  streamMessage,
} from '@/api/chat'

/** 发送结果（D16）：只有 `rejected`（响应前明确未受理）允许恢复草稿；
 *  `disconnected` 是 SSE 已建立后的异常，恢复会造成重复用户消息。 */
export interface SendResult {
  outcome: 'accepted' | 'rejected' | 'disconnected' | 'invalid'
  message: string
}

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
  write_draft_on_candidate: '按候选写稿',
  search_hotspots: '搜索热点',
  fetch_url: '读取网页',
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
  web_search: '搜索热点',
  web_fetch: '读取网页',
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
  // 创建会话在途（D15）：同时约束侧栏按钮、composer 与 init()，避免一次操作建多条会话。
  const creatingSession = ref(false)
  const streamText = ref('')
  const toolSteps = ref<ToolStep[]>([])
  const error = ref<string | null>(null)

  // 上下文状态（S7.24）：生效 / 待确认事实与版本；刷新和每轮结束后都从服务端取回
  const context = ref<ChatContext | null>(null)
  // v2 §3.3B：下一次改稿的基础版本绑定（只影响下一次操作，不切 head）
  const selectedBase = ref<{ objectKey: string; versionId: string; versionNo: number } | null>(
    null,
  )
  const contextLoading = ref(false)
  const contextError = ref<string | null>(null)
  const artifactVersions = ref<ArtifactVersion[]>([])
  const artifactPreview = ref<ArtifactVersion | null>(null)
  const artifactObjectKey = ref('')
  const artifactsLoading = ref(false)
  const artifactsError = ref<string | null>(null)
  const sourcePreview = ref<ChatMessage | null>(null)
  const sourceError = ref<string | null>(null)
  let sessionEpoch = 0
  // 会话列表的 generation / 在途请求 / 脏标记（D15）：mutation 后强制刷新，
  // 旧列表响应不得覆盖创建、删除或重命名后的状态。
  let listGeneration = 0
  let listInFlight: Promise<void> | null = null
  let listDirty = false
  let contextRequest = 0
  let artifactRequest = 0
  let previewRequest = 0
  let streamAbort: AbortController | undefined

  function sameSession(id: string, epoch: number): boolean {
    return currentId.value === id && sessionEpoch === epoch
  }

  function clearSessionView(): void {
    sessionEpoch += 1
    contextRequest += 1
    artifactRequest += 1
    previewRequest += 1
    streamAbort?.abort()
    streamAbort = undefined
    sending.value = false
    context.value = null
    selectedBase.value = null
    contextError.value = null
    contextLoading.value = false
    artifactVersions.value = []
    artifactPreview.value = null
    artifactObjectKey.value = ''
    artifactsError.value = null
    artifactsLoading.value = false
    sourcePreview.value = null
    sourceError.value = null
    messages.value = []
    runs.value = {}
  }

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
    const epoch = sessionEpoch
    if (sessionId === null) {
      stopPolling()
      return
    }
    try {
      const detail = await getChatSession(sessionId)
      // E7OBS4-04：旧请求返回前验证范围——切会话后迟到的响应不能覆盖新会话
      if (!sameSession(sessionId, epoch)) return
      messages.value = detail.messages
      syncProgressFromMessages(detail.messages)
      if (!hasRunning(detail.messages)) stopPolling()
    } catch {
      if (sameSession(sessionId, epoch)) stopPolling()
    }
  }

  /** 拉取会话上下文（S7.24）：面板、待确认项与恢复缺口都来自它。 */
  async function loadContext(): Promise<void> {
    const sessionId = currentId.value
    const epoch = sessionEpoch
    const request = ++contextRequest
    if (sessionId === null) {
      context.value = null
      return
    }
    contextLoading.value = true
    contextError.value = null
    try {
      const loaded = await getChatContext(sessionId)
      if (!sameSession(sessionId, epoch) || request !== contextRequest) return
      if (context.value === null || loaded.state_version >= context.value.state_version) context.value = loaded
    } catch (err) {
      if (sameSession(sessionId, epoch) && request === contextRequest) contextError.value = describeError(err)
    } finally {
      if (sameSession(sessionId, epoch) && request === contextRequest) contextLoading.value = false
    }
  }

  /** 上下文动作（v2 §3.3A）：confirm / reject / retract / replace / reset_topic /
   *  select_object / recover / complete_recovery。
   *
   *  带 `expected_state_version` 做 CAS、带 `action_id` 做客户端幂等：409 时刷新面板并
   *  返回「状态已变化」，由用户重新操作——前端不隐藏选项代替服务端校验，也不静默重放。
   */
  async function actOnContext(
    action: ContextActionRequest['action'],
    targetId?: string,
    value?: string,
    extra?: Partial<ContextActionRequest>,
  ): Promise<string | null> {
    const sessionId = currentId.value
    const current = context.value
    const epoch = sessionEpoch
    if (sessionId === null || current === null) return '上下文还没加载好，请稍后重试'
    try {
      const body: ContextActionRequest = {
        action,
        expected_state_version: current.state_version,
        action_id: crypto.randomUUID(),
      }
      if (targetId !== undefined && targetId !== '') body.target_id = targetId
      if (value !== undefined && value !== '') body.value = value
      if (extra) Object.assign(body, extra)
      const result = await postChatContextAction(sessionId, body)
      if (!sameSession(sessionId, epoch)) return null
      if (context.value === null || result.context.state_version >= context.value.state_version) context.value = result.context
      if (action === 'reset_topic') {
        selectedBase.value = null
        artifactPreview.value = null
      }
      if (result.status === 'failed') return '这次操作未完成，请核对当前状态后重试'
      return null
    } catch (err) {
      if (!sameSession(sessionId, epoch)) return null
      if (err instanceof ApiRequestError && err.httpStatus === 409) {
        await loadContext()
        return '状态已变化，已刷新当前版本，请重新操作'
      }
      return describeError(err)
    }
  }

  /** 开始 / 继续旧会话恢复（每页一次付费模型调用，不自动重试）。 */
  async function startRecovery(): Promise<string | null> {
    const recovery = context.value?.recovery
    if (recovery?.status === 'running') {
      await loadContext()
      return '恢复正在处理，请等待后刷新状态'
    }
    return actOnContext('recover', undefined, undefined, {
      recovery_id: recovery?.recovery_id ?? undefined,
      retry_of: recovery && ['failed', 'interrupted'].includes(recovery.status)
        ? recovery.page_action_id ?? undefined : undefined,
    })
  }

  /** 完成恢复：候选清零；有历史缺口时必须显式确认已核对。 */
  async function completeRecovery(acknowledgePartialHistory = false): Promise<string | null> {
    return actOnContext('complete_recovery', undefined, undefined,
                        { acknowledge_partial_history: acknowledgePartialHistory,
                          recovery_id: context.value?.recovery?.recovery_id ?? undefined })

  }

  async function loadArtifacts(objectKey: string): Promise<void> {
    const id = currentId.value
    if (id === null) return
    const epoch = sessionEpoch
    const request = ++artifactRequest
    artifactObjectKey.value = objectKey
    artifactVersions.value = []
    artifactPreview.value = null
    artifactsLoading.value = true
    artifactsError.value = null
    try {
      const result = await listChatArtifacts(id, objectKey)
      if (sameSession(id, epoch) && request === artifactRequest) artifactVersions.value = result.items
    } catch (err) {
      if (sameSession(id, epoch) && request === artifactRequest) artifactsError.value = describeError(err)
    } finally {
      if (sameSession(id, epoch) && request === artifactRequest) artifactsLoading.value = false
    }
  }

  async function previewArtifact(versionId: string): Promise<void> {
    const id = currentId.value
    if (id === null) return
    const epoch = sessionEpoch
    const request = ++previewRequest
    artifactPreview.value = null
    artifactsError.value = null
    try {
      const result = await getChatArtifact(id, versionId)
      if (sameSession(id, epoch) && request === previewRequest) artifactPreview.value = result
    } catch (err) {
      if (sameSession(id, epoch) && request === previewRequest) artifactsError.value = describeError(err)
    }
  }

  async function chooseClue(versionId: string): Promise<string | null> {
    const id = currentId.value
    const current = context.value
    const epoch = sessionEpoch
    if (id === null || current === null) return '请先加载当前对话'
    try {
      const result = await selectClueVersion(id, versionId, {
        action_id: crypto.randomUUID(), expected_state_version: current.state_version,
      })
      if (sameSession(id, epoch)) context.value = result.context
      return null
    } catch (err) {
      if (!sameSession(id, epoch)) return null
      if (err instanceof ApiRequestError && err.httpStatus === 409) await loadContext()
      return describeError(err)
    }
  }

  async function saveClue(content: Record<string, unknown>): Promise<string | null> {
    const id = currentId.value
    const current = context.value
    const base = artifactPreview.value
    const epoch = sessionEpoch
    if (id === null || current === null || base?.object_type !== 'clue') return '请先选择要编辑的线索版本'
    const head = current.artifact_heads.find(item => item.object_key === base.object_key)
    try {
      const result = await createClueVersion(id, {
        action_id: crypto.randomUUID(), expected_state_version: current.state_version,
        expected_head_version_id: head?.version_id ?? null,
        base_version_id: base.version_id, object_key: base.object_key, content,
      })
      if (!sameSession(id, epoch)) return null
      context.value = result.context
      await loadArtifacts(base.object_key)
      if (sameSession(id, epoch) && result.version_id) await previewArtifact(result.version_id)
      return null
    } catch (err) {
      if (!sameSession(id, epoch)) return null
      if (err instanceof ApiRequestError && err.httpStatus === 409) await loadContext()
      return describeError(err)
    }
  }

  async function reparseClue(): Promise<string | null> {
    const base = artifactPreview.value
    const current = context.value
    if (base?.object_type !== 'clue' || current === null) return '请先选择图片线索'
    const result = await send({ text: '请重新解析这组图片，生成一份新的原始线索。',
      operationBinding: JSON.stringify({
        operation: 'parse_clue_from_image', object_key: base.object_key,
        force_reparse: true, expected_state_version: current.state_version,
      }) })
    return result.outcome === 'accepted' ? null : result.message
  }

  async function showSource(messageId: string): Promise<void> {
    const id = currentId.value
    if (id === null) return
    const epoch = sessionEpoch
    sourcePreview.value = null
    sourceError.value = null
    const existing = messages.value.find(item => item.message_id === messageId)
    if (existing) { sourcePreview.value = existing; return }
    try {
      const detail = await getChatSession(id)
      if (!sameSession(id, epoch)) return
      sourcePreview.value = detail.messages.find(item => item.message_id === messageId) ?? null
      if (sourcePreview.value === null) sourceError.value = '这条来源消息已不可用'
    } catch (err) {
      if (sameSession(id, epoch)) sourceError.value = describeError(err)
    }
  }

  /** 删除指定会话（不传 = 删当前）。204 空响应由 `api/client.ts` 处理。
   *  删当前会话清空消息视图；删别的会话保持当前会话；两者都强制刷新列表（D11 / D15）。 */
  async function removeSession(targetId?: string): Promise<string | null> {
    const id = targetId ?? currentId.value
    if (id === null) return '没有可删除的会话'
    const isCurrent = id === currentId.value
    const epoch = sessionEpoch
    try {
      await deleteChatSession(id)
    } catch (err) {
      return describeError(err)
    }
    invalidateList()
    if (isCurrent && sameSession(id, epoch)) reset()
    await loadSessions(true)
    return null
  }

  /** 重命名成功后原地替换列表项标题：不重排、不重新拉列表（D12）。 */
  async function renameSession(sessionId: string, title: string): Promise<string | null> {
    try {
      const updated = await renameChatSession(sessionId, title)
      invalidateList()       // 作废在途列表请求，避免旧响应把新标题覆盖回去
      sessions.value = sessions.value.map(
        (item) => item.session_id === sessionId ? { ...item, title: updated.title } : item,
      )
      return null
    } catch (err) {
      return describeError(err)
    }
  }

  /** 作废在途列表请求：旧 generation 的响应不再回写，同时收掉 loading。
   *  否则它的 finally 因 generation 不匹配不会复位，"加载中…"会一直挂着。 */
  function invalidateList(): void {
    listGeneration += 1
    sessionsLoading.value = false
  }

  async function fetchSessions(): Promise<void> {
    const generation = ++listGeneration
    try {
      const listing = await listChatSessions({ limit: SESSION_PAGE_SIZE, offset: 0 })
      // 旧 generation 的响应直接丢弃：改名 / 删除 / 创建之后不能再写回旧快照。
      if (generation === listGeneration) {
        sessions.value = listing.items
        sessionsError.value = null
      }
    } catch (err) {
      if (generation === listGeneration) sessionsError.value = describeError(err)
    } finally {
      if (generation === listGeneration) sessionsLoading.value = false
    }
  }

  /** 普通加载共享在途请求；`force` 用于 mutation 后的强制刷新：
   *  在途请求标脏，等它结束后至少再拉一次，绝不复用旧快照（D15）。 */
  async function loadSessions(force = false): Promise<void> {
    if (listInFlight !== null) {
      if (force) listDirty = true
      return listInFlight
    }
    sessionsLoading.value = true
    sessionsError.value = null
    listDirty = false
    const task = (async () => {
      await fetchSessions()
      if (listDirty) {
        listDirty = false
        await fetchSessions()
      }
    })()
    listInFlight = task
    try {
      await task
    } finally {
      listInFlight = null
    }
  }

  async function newSession(): Promise<string | null> {
    if (creatingSession.value) return '正在创建会话，请稍候'
    stopPolling()
    clearSessionView()
    currentId.value = null
    creatingSession.value = true
    const epoch = sessionEpoch
    try {
      const created = await createChatSession()
      // 创建期间被切会话打断时不能静默报成功：调用方必须知道这一步没生效。
      if (sessionEpoch !== epoch) {
        error.value = '会话创建被打断，请重试'
        return error.value
      }
      currentId.value = created.session_id
      messages.value = []
      streamText.value = ''
      toolSteps.value = []
      progress.value = null
      progressScope.value = { messageId: '', turnId: '' }
      error.value = null
      context.value = null
      await loadSessions(true)
      return null
    } catch (err) {
      if (sessionEpoch !== epoch) {
        error.value = '会话创建被打断，请重试'
        return error.value
      }
      error.value = describeError(err)
      return error.value
    } finally {
      creatingSession.value = false
    }
  }

  // 进页面时：拉会话列表；如果最近有一条会话就顺手打开它——刷新后消息仍在（S7.5 的验收口径）。
  async function init(): Promise<void> {
    await loadSessions()
    const latest = sessions.value[0]
    if (currentId.value === null && !creatingSession.value && latest !== undefined) {
      await openSession(latest.session_id)
    }
  }

  async function openSession(sessionId: string): Promise<void> {
    stopPolling()
    clearSessionView()
    currentId.value = sessionId
    const epoch = sessionEpoch
    messagesLoading.value = true
    error.value = null
    streamText.value = ''
    toolSteps.value = []
    progress.value = null
    progressScope.value = { messageId: '', turnId: '' }
    try {
      const detail = await getChatSession(sessionId)
      if (!sameSession(sessionId, epoch)) return
      messages.value = detail.messages
      syncProgressFromMessages(detail.messages)
      if (hasRunning(detail.messages)) startPolling()
      void loadContext()
    } catch (err) {
      if (!sameSession(sessionId, epoch)) return
      messages.value = []
      error.value = describeError(err)
    } finally {
      if (sameSession(sessionId, epoch)) messagesLoading.value = false
    }
  }

  async function refreshMessages(): Promise<void> {
    const sessionId = currentId.value
    const epoch = sessionEpoch
    if (sessionId === null) return
    try {
      const detail = await getChatSession(sessionId)
      if (!sameSession(sessionId, epoch)) return
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

  /** 发一条消息，返回结构化结果（D16）。调用方只对 `rejected` 恢复草稿：
   *  SSE 已建立后的断流不能引导重复提交，`invalid` 是前置校验失败、草稿原样保留。 */
  async function send(input: ChatStreamInput & { text: string }): Promise<SendResult> {
    const text = input.text.trim()
    const files = input.files ?? []
    if (text.length === 0 && files.length === 0) {
      return { outcome: 'invalid', message: '请先说点什么，或者丢图进来' }
    }
    if (sending.value) return { outcome: 'invalid', message: '上一轮还在跑，等它说完再发' }
    if (creatingSession.value) return { outcome: 'invalid', message: '正在创建会话，请稍候' }

    if (currentId.value === null) {
      const invalid = await newSession()
      if (invalid !== null) return { outcome: 'invalid', message: invalid }
    }
    const sessionId = currentId.value
    const epoch = sessionEpoch
    if (sessionId === null) {
      return { outcome: 'invalid', message: '会话没能建起来，请重试' }
    }

    sending.value = true
    error.value = null
    streamText.value = ''
    toolSteps.value = []
    progress.value = null
    progressScope.value = { messageId: '', turnId: '' }
    let accepted = false
    try {
      // v2 §3.3B：选择了基础版本时携带结构化绑定（服务端校验后随用户消息持久保存）
      const binding = input.operationBinding ?? (selectedBase.value && context.value !== null
        ? JSON.stringify({
            operation: 'revise_draft',
            object_key: selectedBase.value.objectKey,
            base_version_id: selectedBase.value.versionId,
            expected_state_version: context.value.state_version,
          })
        : undefined)
      streamAbort = new AbortController()
      await streamMessage(sessionId, { text, files, operationBinding: binding },
                          (event) => {
        accepted = true
        if (sameSession(sessionId, epoch)) onStreamEvent(event.event, event.data)
      }, streamAbort.signal)
      if (!sameSession(sessionId, epoch)) return { outcome: 'invalid', message: '' }
      selectedBase.value = null   // 绑定只作用于下一次明确操作
      return { outcome: 'accepted', message: '' }
    } catch (err) {
      if (!sameSession(sessionId, epoch)) return { outcome: 'invalid', message: '' }
      const message = describeError(err)
      error.value = message
      // 只有「响应前、明确的 HTTP 失败」才算确认未受理；流已开始后的一切异常按断流处理。
      const confirmedRejected = !accepted && err instanceof ApiRequestError
      return { outcome: confirmedRejected ? 'rejected' : 'disconnected', message }
    } finally {
      if (sameSession(sessionId, epoch)) {
        sending.value = false
        streamText.value = ''
        await refreshMessages()
        if (sameSession(sessionId, epoch)) await loadContext()
        if (sameSession(sessionId, epoch)) await loadSessions(true)
        const latest = sameSession(sessionId, epoch)
          ? [...messages.value].reverse().find((item) => item.run_id) : undefined
        if (latest?.run_id) void ensureRun(latest.run_id)
      }
    }
  }

  // 取一次结果卡片的数据并缓存；409（还没写回）折成 null，不重复请求。
  async function ensureRun(runId: string): Promise<RunDetail | null> {
    const epoch = sessionEpoch
    const cached = runs.value[runId]
    if (cached !== undefined) return cached
    try {
      const detail = await getRun(runId)
      if (sessionEpoch === epoch) runs.value[runId] = detail
      return detail
    } catch {
      if (sessionEpoch === epoch) runs.value[runId] = null
      return null
    }
  }

  function reset(): void {
    stopPolling()
    stopClock()
    clearSessionView()
    currentId.value = null
    context.value = null
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
    sending, busy, creatingSession, streamText, toolSteps, error, runs,
    progress, stageElapsedMs, stageLongRunning,
    context, contextLoading, contextError, selectedBase,
    artifactVersions, artifactPreview, artifactObjectKey, artifactsLoading, artifactsError,
    sourcePreview, sourceError,
    loadArtifacts, previewArtifact, chooseClue, saveClue, reparseClue, showSource,
    loadSessions, loadContext, actOnContext, startRecovery, completeRecovery,
    removeSession, renameSession,
    init, newSession, openSession, refreshMessages, send, ensureRun, reset,
    stopPolling, toolLabel,
  }
})
