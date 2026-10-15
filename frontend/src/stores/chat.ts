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

/** 当前阶段视图（S7.21）：来自 SSE `stage` 事件或刷新后的 `message.progress` 快照。 */
export interface StageView {
  stage: string
  label: string
  status: string
  attempt: number
  startedAt: string
  errorCategory: string
}

/** 跑起来超过这个时长就提示「耗时较长」——只是提示，不据此断定失败。 */
export const LONG_STAGE_MS = 60_000

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

  /** 把一份「类快照」对象合进当前阶段（SSE 事件或 `message.progress` 都走这里）。 */
  function applyProgressLike(raw: Record<string, unknown> | null | undefined): void {
    if (raw === null || raw === undefined) return
    const stage = String(raw.stage ?? '')
    if (stage === '') return
    const attempt = Number(raw.attempt ?? 1)
    const previous = progress.value
    const sameStage = previous !== null && previous.stage === stage && previous.attempt === attempt
    const startedAt = String(raw.started_at ?? '') || (sameStage ? previous.startedAt : '')
    progress.value = {
      stage,
      label: String(raw.label ?? '') || stageLabel(stage),
      status: String(raw.status ?? 'running'),
      attempt,
      // 事件里可能只有 updated_at（如 tool_progress）：同阶段沿用旧起点，新阶段从此刻起算
      startedAt: startedAt !== '' ? startedAt : new Date().toISOString(),
      errorCategory: String(raw.error_category ?? ''),
    }
    if (progress.value.status === 'running') startClock()
    else stopClock()
  }

  /** 刷新 / 轮询后：从最近一条带快照的 assistant 消息恢复阶段；没有就清空。 */
  function syncProgressFromMessages(items: ChatMessage[]): void {
    const latest = [...items].reverse().find(
      (item) => item.role === 'assistant'
        && item.progress !== null && item.progress !== undefined
        && String(item.progress.stage ?? '') !== '',
    )
    if (latest?.progress) {
      applyProgressLike(latest.progress as unknown as Record<string, unknown>)
    } else if (!hasRunning(items)) {
      progress.value = null
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
    if (currentId.value === null) {
      stopPolling()
      return
    }
    try {
      const detail = await getChatSession(currentId.value)
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
    try {
      const detail = await getChatSession(sessionId)
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
    if (currentId.value === null) return
    try {
      const detail = await getChatSession(currentId.value)
      messages.value = detail.messages
      syncProgressFromMessages(detail.messages)
    } catch {
      // 刷新失败不打断这一轮：流式增量仍在屏幕上，终态再对齐一次
    }
  }

  function onStreamEvent(event: string, data: Record<string, unknown>): void {
    switch (event) {
      case 'turn_started':
        void refreshMessages()
        break
      case 'stage':
        // 阶段生命周期（理解与决策 / 整理回复）：刷新后由 message.progress 恢复同一份快照
        applyProgressLike(data)
        break
      case 'tool_started':
        toolSteps.value.push({ stage: `开始：${toolLabel(String(data.tool ?? ''))}`, done: 0, total: 0 })
        break
      case 'tool_progress':
        toolSteps.value.push({
          stage: String(data.stage ?? ''),
          done: Number(data.done ?? 0),
          total: Number(data.total ?? 0),
        })
        if (String(data.stage_key ?? '') !== '') {
          // 排队等待 / 素材同步 / 向量回填 / 五节点：以阶段键更新当前阶段
          applyProgressLike({
            stage: String(data.stage_key),
            label: stageLabel(String(data.stage_key)),
            status: 'running',
            attempt: data.attempt,
            updated_at: data.updated_at,
          })
        }
        break
      case 'tool_finished':
        toolSteps.value.push({
          stage: data.status === 'succeeded' ? '工具完成' : `工具失败：${String(data.error ?? '')}`,
          done: 0,
          total: 0,
        })
        break
      case 'text_delta':
        streamText.value += String(data.text ?? '')
        break
      case 'error':
        error.value = String(data.message ?? '这一轮失败了')
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
