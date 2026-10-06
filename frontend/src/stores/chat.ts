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

// 工具名的中文标签（工具 schema 的单一来源是后端 services/chat_tools.py::TOOLS）。
const TOOL_LABELS: Record<string, string> = {
  run_hotspot_analysis: '跑完整分析',
}

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name
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
    try {
      const detail = await getChatSession(sessionId)
      messages.value = detail.messages
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
    } catch {
      // 刷新失败不打断这一轮：流式增量仍在屏幕上，终态再对齐一次
    }
  }

  function onStreamEvent(event: string, data: Record<string, unknown>): void {
    switch (event) {
      case 'turn_started':
        void refreshMessages()
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
  async function send(input: { text: string; file?: File | null }): Promise<string | null> {
    const text = input.text.trim()
    const file = input.file ?? null
    if (text.length === 0 && file === null) return '请先说点什么，或者丢一张图进来'
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
    try {
      await streamMessage(sessionId, { text, file }, (event) => {
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
    currentId.value = null
    messages.value = []
    streamText.value = ''
    toolSteps.value = []
    error.value = null
  }

  return {
    sessions, sessionsLoading, sessionsError,
    currentId, currentSession, messages, messagesLoading,
    sending, busy, streamText, toolSteps, error, runs,
    loadSessions, init, newSession, openSession, refreshMessages, send, ensureRun, reset,
    stopPolling, toolLabel,
  }
})
