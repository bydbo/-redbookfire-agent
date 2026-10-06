import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import type { ChatMessage, ChatSessionDetail, ChatSessionList } from '@/api/chat'
import { createChatSession, getChatSession, listChatSessions, streamMessage } from '@/api/chat'
import { useChatStore } from '@/stores/chat'

vi.mock('@/api/chat', () => ({
  createChatSession: vi.fn(),
  listChatSessions: vi.fn(),
  getChatSession: vi.fn(),
  streamMessage: vi.fn(),
}))

vi.mock('@/api/analysis', () => ({ getRun: vi.fn() }))

const POLL_MS = 2000

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    message_id: 'm-1',
    session_id: 's-1',
    role: 'assistant',
    content: '',
    status: 'succeeded',
    attachments: [],
    tool_calls: [],
    prompt_versions: {},
    created_at: '2026-10-06T04:00:00+00:00',
    cost_cny: 0,
    latency_ms: 0,
    ...overrides,
  }
}

function detail(messages: ChatMessage[]): ChatSessionDetail {
  return {
    session_id: 's-1',
    title: '帮我蹭一下这个热点',
    created_at: '2026-10-06T04:00:00+00:00',
    updated_at: '2026-10-06T04:00:00+00:00',
    messages,
  }
}

function listing(): ChatSessionList {
  return {
    items: [{
      session_id: 's-1',
      title: '帮我蹭一下这个热点',
      created_at: '2026-10-06T04:00:00+00:00',
      updated_at: '2026-10-06T04:00:00+00:00',
      message_count: 2,
      last_message_preview: '跑了完整分析，命中 5 条。',
    }],
    total: 1,
  }
}

function runDetail(): RunDetail {
  return {
    run_id: 'r-1',
    status: 'succeeded',
    created_at: '2026-10-06T04:00:00+00:00',
    totals: { llm_calls: 3, prompt_tokens: 10, completion_tokens: 5, cost_cny: 0.05, latency_ms: 1200 },
    prompt_versions: {},
    hotspots: [],
  }
}

describe('useChatStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.mocked(createChatSession).mockReset()
    vi.mocked(listChatSessions).mockReset()
    vi.mocked(getChatSession).mockReset()
    vi.mocked(streamMessage).mockReset()
    vi.mocked(getRun).mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('会话列表与「新对话」', async () => {
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-2', title: '', created_at: '', updated_at: '',
    })
    const store = useChatStore()

    await store.loadSessions()
    expect(store.sessions).toHaveLength(1)
    expect(store.currentSession).toBeNull()

    await store.newSession()
    expect(store.currentId).toBe('s-2')
    expect(store.messages).toEqual([])
  })

  it('会话列表拉失败时只记错误，不清空已有列表', async () => {
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    const store = useChatStore()
    await store.loadSessions()

    vi.mocked(listChatSessions).mockRejectedValue(new Error('503'))
    await store.loadSessions()
    expect(store.sessionsError).toBe('503')
    expect(store.sessions).toHaveLength(1)
  })

  it('打开会话：running 的消息会开轮询，落终态后停', async () => {
    vi.mocked(getChatSession).mockResolvedValueOnce(detail([message({ status: 'running' })]))
    const store = useChatStore()

    await store.openSession('s-1')
    expect(store.messages[0].status).toBe('running')

    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ status: 'succeeded', content: '跑完了' }),
    ]))
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(store.messages[0].content).toBe('跑完了')

    const calls = vi.mocked(getChatSession).mock.calls.length
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(vi.mocked(getChatSession).mock.calls.length).toBe(calls)   // 终态后不再轮询
  })

  it('空消息被拦下，不发请求', async () => {
    const store = useChatStore()
    await expect(store.send({ text: '   ' })).resolves.toBe('请先说点什么，或者丢一张图进来')
    expect(streamMessage).not.toHaveBeenCalled()
  })

  it('第一次发送会先建会话，再逐帧归约事件，最后回读消息', async () => {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getRun).mockResolvedValue(runDetail())
    const observed: { text: string; steps: string[] } = { text: '', steps: [] }
    const answered = [
      message({ message_id: 'm-u', role: 'user', content: '帮我蹭一下这个热点' }),
      message({ message_id: 'm-a', run_id: 'r-1', content: '跑了完整分析，命中 5 条。' }),
    ]
    vi.mocked(getChatSession).mockResolvedValue(detail(answered))
    vi.mocked(streamMessage).mockImplementation(async (_id, _input, onEvent) => {
      onEvent({ event: 'turn_started', data: {} })
      onEvent({ event: 'tool_started', data: { tool: 'run_hotspot_analysis' } })
      onEvent({ event: 'tool_progress', data: { stage: '分析中', done: 50, total: 100 } })
      onEvent({ event: 'tool_finished', data: { status: 'succeeded' } })
      onEvent({ event: 'text_delta', data: { text: '跑' } })
      onEvent({ event: 'text_delta', data: { text: '好了' } })
      const store = useChatStore()
      observed.text = store.streamText
      observed.steps = store.toolSteps.map((step) => step.stage)
      onEvent({ event: 'turn_finished', data: { status: 'succeeded' } })
    })

    const store = useChatStore()
    await expect(store.send({ text: '帮我蹭一下这个热点' })).resolves.toBeNull()

    expect(createChatSession).toHaveBeenCalledTimes(1)
    expect(observed.text).toBe('跑好了')
    expect(observed.steps).toEqual(['开始：跑完整分析', '分析中', '工具完成'])
    expect(store.messages.map((item) => item.message_id)).toEqual(['m-u', 'm-a'])
    expect(store.streamText).toBe('')                 // 终态后清掉增量，显示落库正文
    expect(getRun).toHaveBeenCalledWith('r-1')        // 顺手取结果卡片
  })

  it('流式请求报错时记错误，但消息仍以库里的为准', async () => {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ message_id: 'm-u', role: 'user', content: '帮我蹭一下' }),
    ]))
    vi.mocked(streamMessage).mockRejectedValue(new Error('网络错误：上传失败'))
    const store = useChatStore()

    await store.send({ text: '帮我蹭一下' })
    expect(store.error).toBe('网络错误：上传失败')
    expect(store.messages).toHaveLength(1)
    expect(store.sending).toBe(false)
  })

  it('结果卡片只取一次，取不到就缓存 null', async () => {
    vi.mocked(getRun).mockResolvedValueOnce(runDetail())
    const store = useChatStore()

    await store.ensureRun('r-1')
    await store.ensureRun('r-1')
    expect(getRun).toHaveBeenCalledTimes(1)

    vi.mocked(getRun).mockRejectedValueOnce(new Error('409'))
    await expect(store.ensureRun('r-2')).resolves.toBeNull()
    await store.ensureRun('r-2')
    expect(getRun).toHaveBeenCalledTimes(2)
  })
})
