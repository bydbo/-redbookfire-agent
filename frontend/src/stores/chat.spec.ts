import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import type {
  ChatContext,
  ChatMessage,
  ChatSessionDetail,
  ChatSessionList,
} from '@/api/chat'
import {
  createChatSession,
  getChatContext,
  getChatSession,
  listChatSessions,
  postChatContextAction,
  streamMessage,
} from '@/api/chat'
import { ApiRequestError } from '@/api/client'
import { TOOL_LABELS, errorCategoryLabel, stageLabel, toolLabel, useChatStore } from '@/stores/chat'

vi.mock('@/api/chat', () => ({
  createChatSession: vi.fn(),
  listChatSessions: vi.fn(),
  getChatSession: vi.fn(),
  getChatContext: vi.fn(),
  postChatContextAction: vi.fn(),
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

describe('useChatStore 阶段身份隔离（R5 / E7OBS2-02）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })
  afterEach(() => vi.useRealTimers())

  it('另一条执行的迟到终态不回退；同 stage 新 run 收旧 run 结束也不改写', async () => {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([]))
    const observed: Record<string, string | undefined> = {}
    vi.mocked(streamMessage).mockImplementation(async (_id, _input, onEvent) => {
      onEvent({ event: 'tool_progress', data: { stage_key: 'report', stage: '报告',
                status: 'running', attempt: 1, run_id: 'run-new',
                started_at: '2026-10-15T08:00:00+00:00' } })
      // 旧 run 的迟到终态（跨 stage）：不许回退，也不许串身份
      onEvent({ event: 'tool_progress', data: { stage_key: 'draft', stage: '撰稿',
                status: 'succeeded', attempt: 1, run_id: 'run-old' } })
      const store = useChatStore()
      observed.stage = store.progress?.stage
      observed.runId = store.progress?.runId
      // 同 stage、新 run 收旧 run 结束：同样不改写当前状态
      onEvent({ event: 'tool_progress', data: { stage_key: 'report', stage: '报告',
                status: 'failed', attempt: 9, run_id: 'run-old',
                error_category: 'llm_error' } })
      observed.status = store.progress?.status
      observed.reportRun = store.progress?.runId
      // 同一 run 的合法推进照常：report 成功
      onEvent({ event: 'tool_progress', data: { stage_key: 'report', stage: '报告',
                status: 'succeeded', attempt: 1, run_id: 'run-new' } })
      observed.finalStatus = store.progress?.status
      // 新阶段不带身份（对话侧收尾）：run 身份清空，不残留
      onEvent({ event: 'stage', data: { stage: 'final_reply', label: '整理回复',
                status: 'succeeded', attempt: 1 } })
      observed.finalRun = store.progress?.runId
    })
    const store = useChatStore()

    await store.send({ text: '帮我蹭一下这个热点' })

    expect(observed.stage).toBe('report')            // 旧 draft 终态不回退
    expect(observed.runId).toBe('run-new')           // 身份不串
    expect(observed.status).toBe('running')          // 旧 run 同 stage 终态不改写
    expect(observed.reportRun).toBe('run-new')
    expect(observed.finalStatus).toBe('succeeded')   // 同 run 推进照常
    expect(observed.finalRun).toBe('')               // 跨阶段缺身份：清空
  })
})

describe('权威快照投影（E7OBS4-04）', () => {
  function snapshot(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      stage: 'report',
      label: '报告',
      status: 'running',
      attempt: 1,
      started_at: '2026-10-06T04:00:00+00:00',
      updated_at: '2026-10-06T04:00:01+00:00',
      error_category: '',
      run_id: 'run-a',
      job_id: null,
      source_id: 'worker:run-a:g0',
      stage_instance_id: 'worker:run-a:g0:report:1',
      source_seq: 3,
      progress_revision: 5,
      nodes: [],
      ...overrides,
    }
  }

  const envelope = { message_id: 'm-a', turn_id: 't-1' }

  /** 发一轮事件并在流中途取一份快照（`send()` 之后会回读消息，终态会以库里为准）。 */
  async function play(
    events: { event: string; data: Record<string, unknown> }[],
    observe: () => void,
  ): Promise<ReturnType<typeof useChatStore>> {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([message({ status: 'running' })]))
    vi.mocked(streamMessage).mockImplementation(async (_id, _input, onEvent) => {
      for (const item of events) onEvent(item)
      observe()
    })
    const store = useChatStore()
    await store.send({ text: '帮我蹭一下这个热点' })
    return store
  }

  it('D1：原始阶段是旧 draft，嵌套 progress 是当前 report —— 展示 report，不闪回', async () => {
    let stage = ''
    let label = ''
    await play([
      { event: 'tool_progress', data: { ...envelope, stage_key: 'draft', stage: '撰稿',
        status: 'running', progress: snapshot() } },
    ], () => {
      const store = useChatStore()
      stage = store.progress?.stage ?? ''
      label = store.progress?.label ?? ''
    })
    expect(stage).toBe('report')
    expect(label).toBe('报告')
  })

  it('D2：较新 revision 的同名新实例、attempt=1 合法接受（旧终态守卫不误拦）', async () => {
    const observed: string[] = []
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([message({ status: 'running' })]))
    vi.mocked(streamMessage).mockImplementation(async (_id, _input, onEvent) => {
      const store = useChatStore()
      onEvent({ event: 'stage', data: { ...envelope, progress: snapshot({ status: 'succeeded' }) } })
      observed.push(`${store.progress?.status}/${store.progress?.stageInstanceId}`)
      onEvent({ event: 'stage', data: { ...envelope, progress: snapshot({
        status: 'running', attempt: 1, stage_instance_id: 'worker:run-a:g0:report:2',
        progress_revision: 6, source_seq: 4, started_at: '2026-10-06T04:00:05+00:00',
      }) } })
      observed.push(`${store.progress?.status}/${store.progress?.stageInstanceId}`)
    })
    await useChatStore().send({ text: '帮我蹭一下这个热点' })

    // 第一个实例已终结，但更高 revision 的第二个同名实例必须能启动（同 stage、attempt 都是 1）
    expect(observed).toEqual([
      'succeeded/worker:run-a:g0:report:1',
      'running/worker:run-a:g0:report:2',
    ])
  })

  it('D3：乱序 revision 不回退、重复 revision 不重置起点', async () => {
    const observed: { rev: number; startedAt: string }[] = []
    await play([
      { event: 'stage', data: { ...envelope, progress: snapshot({ progress_revision: 7 }) } },
      { event: 'stage', data: { ...envelope, progress: snapshot({
        stage: 'draft', status: 'running', progress_revision: 5,
        started_at: '2026-10-06T03:00:00+00:00',
        stage_instance_id: 'worker:run-a:g0:draft:1',
      }) } },
      { event: 'stage', data: { ...envelope, progress: snapshot({
        progress_revision: 7, started_at: '2026-10-06T05:00:00+00:00',
      }) } },
    ], () => {
      const store = useChatStore()
      observed.push({
        rev: store.progress?.progressRevision ?? 0,
        startedAt: store.progress?.startedAt ?? '',
      })
    })
    expect(observed).toHaveLength(1)
    expect(observed[0].rev).toBe(7)
    expect(observed[0].startedAt).toBe('2026-10-06T04:00:00+00:00')  // 起点不被事件到达时间改写
  })

  it('D4：新消息 revision 更低、切会话后旧响应返回 —— 不跨范围比较、不覆盖新会话', async () => {
    const observed: number[] = []
    await play([
      { event: 'stage', data: { ...envelope, progress: snapshot({ progress_revision: 9 }) } },
      { event: 'stage', data: { message_id: 'm-b', turn_id: 't-2',
        progress: snapshot({ stage: 'clue', label: '拆解', progress_revision: 1 }) } },
    ], () => {
      const store = useChatStore()
      observed.push(store.progress?.progressRevision ?? 0, store.progress?.stage === 'clue' ? 1 : 0)
    })
    expect(observed).toEqual([1, 1])   // 新消息的低 revision 照常接受

    // 切会话后旧请求返回：不覆盖新会话的视图
    setActivePinia(createPinia())
    vi.mocked(getChatSession).mockReset()
    let resolveOld: (value: ChatSessionDetail) => void = () => {}
    const slow = new Promise<ChatSessionDetail>((resolve) => { resolveOld = resolve })
    vi.mocked(getChatSession).mockReturnValueOnce(slow)
    const store = useChatStore()
    const pending = store.openSession('s-1')
    vi.mocked(getChatSession).mockResolvedValue(detail([message({ message_id: 'm-new' })]))
    await store.openSession('s-2')
    resolveOld(detail([message({ message_id: 'm-old', progress: snapshot() as never })]))
    await pending
    expect(store.currentId).toBe('s-2')
    expect(store.messages.map((item) => item.message_id)).toEqual(['m-new'])
  })

  it('D5：SSE + 轮询 + 刷新同一份快照交错 —— 字段一致、不重复回退', async () => {
    let fromSse: Record<string, unknown> = {}
    let fromRefresh: Record<string, unknown> = {}
    const shared = snapshot({ progress_revision: 4, status: 'running' })
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ message_id: 'm-a', turn_id: 't-1', status: 'running',
        progress: shared as never }),
    ]))
    vi.mocked(streamMessage).mockImplementation(async (_id, _input, onEvent) => {
      onEvent({ event: 'stage', data: { ...envelope, progress: shared } })
      const store = useChatStore()
      fromSse = { ...store.progress }
      await store.refreshMessages()
      fromRefresh = { ...store.progress }
    })
    const store = useChatStore()
    await store.send({ text: '帮我蹭一下这个热点' })

    expect(fromSse).toEqual(fromRefresh)
    expect(store.progress?.label).toBe('报告')
    expect(store.progress?.sourceId).toBe('worker:run-a:g0')
    expect(store.progress?.progressRevision).toBe(4)
    expect(store.progress?.startedAt).toBe('2026-10-06T04:00:00+00:00')
  })

  it('D6：失败 / 取消终态带快照 —— 状态如实、计时停止', async () => {
    const observed: { status: string; running: boolean }[] = []
    await play([
      { event: 'stage', data: { ...envelope, progress: snapshot() } },
      { event: 'error', data: { ...envelope, message: '这一轮被取消',
        progress: snapshot({ status: 'interrupted', error_category: 'cancelled',
          progress_revision: 6 }) } },
    ], () => {
      const store = useChatStore()
      observed.push({
        status: store.progress?.status ?? '',
        running: store.stageElapsedMs > 0,
      })
    })
    expect(observed[0].status).toBe('interrupted')
    expect(observed[0].running).toBe(false)
  })

  it('D7：旧消息没有新字段 —— 按兼容规则显示，且不覆盖新格式较新事实', async () => {
    await play([
      { event: 'stage', data: { ...envelope, progress: snapshot({ progress_revision: 8 }) } },
      // 旧后端事件：没有 progress，也没有 revision
      { event: 'tool_progress', data: { ...envelope, stage_key: 'draft', stage: '撰稿',
        status: 'running', attempt: 1 } },
    ], () => {
      const store = useChatStore()
      expect(store.progress?.stage).toBe('report')
      expect(store.progress?.progressRevision).toBe(8)
    })
  })
})

// ---- 上下文面板（S7.24）----

function context(overrides: Partial<ChatContext> = {}): ChatContext {
  return {
    session_id: 's-1',
    state_version: 3,
    recovery_status: 'complete',
    facts: [{
      fact_id: 'f-1',
      kind: 'constraint',
      value: '控制在 300 字以内',
      scope: 'topic',
      scope_id: 'topic:t1',
      status: 'active',
      evidence: '300 字以内',
      source_ref: { message_id: 'm-0' },
      created_version: 1,
    }],
    proposals: [],
    artifact_heads: [],
    effective_mode: 'enforce',
    blocking_proposals: 0,
    ...overrides,
  }
}

describe('chat store · 上下文状态（S7.24）', () => {
  it('打开会话时拉取上下文，供面板展示', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([message()]))
    vi.mocked(getChatContext).mockResolvedValue(context())
    const store = useChatStore()
    await store.openSession('s-1')
    expect(store.context?.state_version).toBe(3)
    expect(store.context?.facts).toHaveLength(1)
  })

  it('确认提案：带 expected_state_version 提交，并应用返回的最新上下文', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([message()]))
    vi.mocked(getChatContext).mockResolvedValue(context({
      proposals: [{
        proposal_id: 'p-1', kind: 'preference', value: '更口语', scope: 'topic',
        reason: 'inferred', conflicts_with: [], created_version: 3,
      }],
      blocking_proposals: 1,
    }))
    const store = useChatStore()
    await store.openSession('s-1')

    vi.mocked(postChatContextAction).mockResolvedValue({
      session_id: 's-1',
      state_version: 4,
      action: 'confirm',
      decision: 'applied',
      reason: '',
      context: context({ state_version: 4, proposals: [], blocking_proposals: 0 }),
    })
    const result = await store.actOnContext('confirm', 'p-1')
    expect(result).toBeNull()
    const body = vi.mocked(postChatContextAction).mock.calls[0][1]
    expect(body).toEqual({ action: 'confirm', expected_state_version: 3, target_id: 'p-1' })
    expect(store.context?.state_version).toBe(4)
    expect(store.context?.proposals).toHaveLength(0)
  })

  it('409 冲突：刷新面板并提示重新操作，不静默重放', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([message()]))
    vi.mocked(getChatContext).mockResolvedValue(context())
    const store = useChatStore()
    await store.openSession('s-1')

    vi.mocked(postChatContextAction).mockRejectedValue(
      new ApiRequestError('状态已变化', 'conflict', 409),
    )
    vi.mocked(getChatContext).mockResolvedValue(context({ state_version: 9 }))
    const result = await store.actOnContext('retract', 'f-1')
    expect(result).toContain('状态已变化')
    expect(store.context?.state_version).toBe(9)
  })
})
