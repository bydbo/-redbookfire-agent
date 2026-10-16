import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import type { ChatMessage, ChatSessionDetail, ChatSessionList } from '@/api/chat'
import { createChatSession, getChatSession, listChatSessions, streamMessage } from '@/api/chat'
import { TOOL_LABELS, errorCategoryLabel, stageLabel, toolLabel, useChatStore } from '@/stores/chat'

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
    await expect(store.send({ text: '   ' })).resolves.toBe('请先说点什么，或者丢图进来')
    expect(streamMessage).not.toHaveBeenCalled()
  })

  it('多张图一起进 multipart（S7.14 的 files 字段）', async () => {
    const store = useChatStore()
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(streamMessage).mockResolvedValue(undefined)
    const files = [
      new File([new Uint8Array([1])], '图1.png', { type: 'image/png' }),
      new File([new Uint8Array([2])], '图2.png', { type: 'image/png' }),
    ]

    await expect(store.send({ text: '', files })).resolves.toBeNull()

    expect(vi.mocked(streamMessage).mock.calls[0][1]).toMatchObject({ text: '', files })
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

  it('工具名有中文标签，没登记的名字原样回落', () => {
    expect(TOOL_LABELS.run_hotspot_analysis).toBe('跑完整分析')
    expect(toolLabel('parse_image_clue')).toBe('解析图片')
    expect(toolLabel('search_materials')).toBe('查素材库')
    expect(toolLabel('read_run')).toBe('读历史运行')
    expect(toolLabel('preview_element_coverage')).toBe('预览爆点要素')
    expect(toolLabel('revise_draft')).toBe('改写文案')
    expect(toolLabel('brand_new_tool')).toBe('brand_new_tool')
  })

  it('阶段与错误类别有中文标签（未登记原样回落）', () => {
    expect(stageLabel('material_sync')).toBe('素材同步')
    expect(stageLabel('final_reply')).toBe('整理回复')
    expect(stageLabel('custom_stage')).toBe('custom_stage')
    expect(errorCategoryLabel('embedding_error')).toBe('向量化失败')
    expect(errorCategoryLabel('internal')).toBe('内部错误')
    expect(errorCategoryLabel('brand_new')).toBe('brand_new')
  })

  it('阶段事件更新当前阶段，tool_progress 的 stage_key 也并入（S7.21）', async () => {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([]))
    const observed: { stage: string; status: string } = { stage: '', status: '' }
    vi.mocked(streamMessage).mockImplementation(async (_id, _input, onEvent) => {
      onEvent({
        event: 'stage',
        data: { stage: 'supervisor', label: '理解与决策', status: 'running', attempt: 1,
                started_at: '2026-10-15T08:00:00+00:00' },
      })
      onEvent({
        event: 'tool_progress',
        data: { stage_key: 'material_sync', stage: '素材同步', status: 'running', attempt: 1 },
      })
      const store = useChatStore()
      observed.stage = store.progress?.stage ?? ''
      onEvent({
        event: 'stage',
        data: { stage: 'final_reply', label: '整理回复', status: 'succeeded', attempt: 1,
                updated_at: '2026-10-15T08:00:05+00:00' },
      })
      observed.status = store.progress?.status ?? ''
    })
    const store = useChatStore()

    await store.send({ text: '帮我蹭一下这个热点' })

    expect(observed.stage).toBe('material_sync')
    expect(observed.status).toBe('succeeded')
    expect(store.stageElapsedMs).toBe(0)              // 终态后不再计时
  })

  it('打开会话时从 message.progress 恢复阶段、尝试次数与失败类别（S7.21）', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({
        status: 'running',
        progress: {
          stage: 'retrieve', label: '检索', status: 'running', attempt: 2,
          started_at: '2026-10-15T08:00:00+00:00', updated_at: '2026-10-15T08:00:03+00:00',
        },
      }),
    ]))
    const store = useChatStore()
    await store.openSession('s-1')

    expect(store.progress?.stage).toBe('retrieve')
    expect(store.progress?.label).toBe('检索')
    expect(store.progress?.attempt).toBe(2)

    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({
        status: 'failed', error: '分析失败',
        progress: {
          stage: 'embedding_backfill', label: '向量回填', status: 'failed', attempt: 1,
          error_category: 'embedding_error', updated_at: '2026-10-15T08:00:04+00:00',
        },
      }),
    ]))
    await store.refreshMessages()

    expect(store.progress?.status).toBe('failed')
    expect(store.progress?.errorCategory).toBe('embedding_error')
    expect(store.stageElapsedMs).toBe(0)
  })
})

describe('useChatStore 阶段单调合并（R3 / OBS-12）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })
  afterEach(() => vi.useRealTimers())

  it('迟到的阶段事件不回退尝试号；同一次执行的终态不因重复事件重开', async () => {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([]))
    const observed: Record<string, string | number | undefined> = {}
    vi.mocked(streamMessage).mockImplementation(async (_id, _input, onEvent) => {
      onEvent({ event: 'stage', data: { stage: 'draft', label: '撰稿', status: 'running',
                attempt: 2, started_at: '2026-10-15T08:00:00+00:00' } })
      onEvent({ event: 'stage', data: { stage: 'draft', label: '撰稿', status: 'running',
                attempt: 1 } })
      const store = useChatStore()
      observed.attempt = store.progress?.attempt           // 仍是 2：旧尝试不回退
      onEvent({ event: 'stage', data: { stage: 'draft', label: '撰稿', status: 'succeeded',
                attempt: 2, updated_at: '2026-10-15T08:00:05+00:00' } })
      observed.status = store.progress?.status             // succeeded
      onEvent({ event: 'stage', data: { stage: 'draft', label: '撰稿', status: 'running',
                attempt: 2 } })
      observed.reopened = store.progress?.status           // 仍 succeeded：终态不重开
      onEvent({ event: 'stage', data: { stage: 'draft', label: '撰稿', status: 'running',
                attempt: 3 } })
      observed.retried = store.progress?.status            // running：更高尝试开启新执行
    })
    const store = useChatStore()

    await store.send({ text: '帮我蹭一下这个热点' })

    expect(observed.attempt).toBe(2)
    expect(observed.status).toBe('succeeded')
    expect(observed.reopened).toBe('succeeded')
    expect(observed.retried).toBe('running')
  })
})
