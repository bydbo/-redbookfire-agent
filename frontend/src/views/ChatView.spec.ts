import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { h, nextTick } from 'vue'

import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import type { ChatMessage, ChatSessionDetail, ChatSessionList } from '@/api/chat'
import { createChatSession, getChatSession, listChatSessions, streamMessage } from '@/api/chat'
import { useChatStore } from '@/stores/chat'
import ChatView from '@/views/ChatView.vue'

vi.mock('@/api/chat', () => ({
  createChatSession: vi.fn(),
  listChatSessions: vi.fn(),
  getChatSession: vi.fn(),
  streamMessage: vi.fn(),
  deleteChatSession: vi.fn(async () => undefined),
  renameChatSession: vi.fn(async (id: string, title: string) => ({
    session_id: id, title, created_at: '', updated_at: '',
  })),
  getChatContext: vi.fn(async () => null),
  chatAttachmentUrl: (session: string, message: string, index: number) =>
    `/api/chat/sessions/${session}/attachments/${message}/${index}`,
}))

vi.mock('@/api/analysis', () => ({
  getRun: vi.fn(),
  getKeyframeUrl: (id: string, index: number) => `/api/materials/${id}/keyframes/${index}`,
  getRunKeyframeUrl: (run: string, position: number, rank: number, index: number) =>
    `/api/runs/${run}/hotspots/${position}/candidates/${rank}/keyframes/${index}`,
  ELEMENT_TYPE_LABELS: {
    ip: 'IP', topic: '主题', scene: '场景', visual: '画面', emotion: '情绪',
    sound: '声音', conflict: '冲突', format: '形式', audience: '人群',
  },
  ELEMENT_TYPES: ['ip', 'topic', 'scene', 'visual', 'emotion', 'sound', 'conflict', 'format', 'audience'],
}))

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
  const element = { type: 'visual' as const, value: '球拍特写', weight: 0.9, confidence: 0.8 }
  return {
    run_id: 'r-1',
    status: 'succeeded',
    created_at: '2026-10-06T04:00:00+00:00',
    totals: { llm_calls: 3, prompt_tokens: 10, completion_tokens: 5, cost_cny: 0.05, latency_ms: 1200 },
    prompt_versions: { hotspot_clue: 1 },
    execution_snapshot: {},
    evidence: {},
    hotspots: [{
      position: 1,
      hotspot_id: 'h-1',
      status: 'succeeded',
      error: null,
      retrieval_meta: {},
      gap_advice: null,
      evidence: {},
      hotspot_raw: '球场上的球拍特写',
      clue: {
        why_it_works: ['日常器材拍出陌生感'],
        mechanisms: [{ name: '反差', explain: '器材特写与运动场景错位' }],
        elements: [element],
        match_keywords: ['球拍'],
        borrow_angles: ['从运动场景切入'],
        risk_notes: [],
      },
      coverage: { ratio: 0.5, covered: [element], gaps: [] },
      candidates: [],
      draft: {
        titles: [{ text: '标题一', style: '直给' }],
        cover_text: '封面字',
        first_3s: '开头三秒',
        body: '正文正文',
        tags: ['#羽毛球'],
        shot_list: ['先拍球场'],
        compliance_notes: ['别带品牌 logo'],
      },
    }],
  }
}

async function mountView() {
  // 隔离挂载必须给 useMessage() 一个 provider（App.vue 顶层在生产环境提供）
  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(ChatView) },
  })
  await flushPromises()
  return wrapper
}

async function clickButton(wrapper: ReturnType<typeof mount>, label: string): Promise<void> {
  const button = wrapper.findAll('button').find((item) => item.text() === label)
  expect(button, `没有找到按钮：${label}`).toBeDefined()
  await button?.trigger('click')
  await flushPromises()
}

/** jsdom 不做布局：scrollHeight / clientHeight 恒为 0，这里显式伪造一份可滚动的度量。 */
function fakeScrollable(
  element: HTMLElement,
  { scrollHeight = 1000, clientHeight = 300 } = {},
): void {
  Object.defineProperty(element, 'scrollHeight', { value: scrollHeight, configurable: true })
  Object.defineProperty(element, 'clientHeight', { value: clientHeight, configurable: true })
}

describe('ChatView（S7.5 对话首页）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(createChatSession).mockReset()
    vi.mocked(listChatSessions).mockReset()
    vi.mocked(getChatSession).mockReset()
    vi.mocked(streamMessage).mockReset()
    vi.mocked(getRun).mockReset()
    vi.mocked(listChatSessions).mockResolvedValue(listing())
    vi.mocked(getChatSession).mockResolvedValue(detail([]))
    vi.mocked(streamMessage).mockResolvedValue(undefined)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('空态给引导，页头只有当前会话标题（历史会话已移到侧栏）', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('还没有消息')
    expect(wrapper.find('h1').text()).toBe('帮我蹭一下这个热点')
    // 刷新恢复：最近一条会话自动打开（消息从库里读回来）
    expect(getChatSession).toHaveBeenCalledWith('s-1')
    // 页头 / 页内都不再有历史会话入口、新建与删除按钮
    expect(wrapper.findAll('.chat-session-item')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('删除会话')
    expect(wrapper.text()).not.toContain('新对话')
  })

  it('库里没有会话时发送一句话：先建会话，再流式，最后按库里消息渲染', async () => {
    vi.mocked(listChatSessions).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ message_id: 'm-u', role: 'user', content: '帮我蹭一下这个热点' }),
      message({
        message_id: 'm-a', content: '跑了完整分析，命中 5 条。',
        tool_calls: [{ tool: 'run_hotspot_analysis', status: 'succeeded', args: {}, run_id: null }],
      }),
    ]))
    const wrapper = await mountView()

    await wrapper.find('textarea').setValue('帮我蹭一下这个热点')
    await clickButton(wrapper, '发送')

    expect(createChatSession).toHaveBeenCalledTimes(1)
    expect(vi.mocked(streamMessage).mock.calls[0][0]).toBe('s-9')
    expect(vi.mocked(streamMessage).mock.calls[0][1]).toMatchObject({ text: '帮我蹭一下这个热点' })
    expect(wrapper.text()).toContain('跑了完整分析，命中 5 条。')
    expect(wrapper.text()).toContain('跑完整分析')
  })

  it('assistant 带 run_id 时把结果卡片渲染出来（与结果页共用同一套）', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ message_id: 'm-u', role: 'user', content: '帮我蹭一下这个热点' }),
      message({
        message_id: 'm-a', content: '跑完了。', run_id: 'r-1',
        tool_calls: [{ tool: 'run_hotspot_analysis', status: 'succeeded', args: {}, run_id: null }],
      }),
    ]))
    vi.mocked(getRun).mockResolvedValue(runDetail())
    const wrapper = await mountView()

    expect(getRun).toHaveBeenCalledWith('r-1')
    expect(wrapper.text()).toContain('爆点要素')
    expect(wrapper.text()).toContain('正文正文')
    expect(wrapper.text()).toContain('文案初稿')
  })

  it('只引用了旧运行的消息不重复贴卡片（read_run / revise_draft 也会带 run_id）', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({
        message_id: 'm-a', content: '跑完了。', run_id: 'r-1',
        tool_calls: [{ tool: 'run_hotspot_analysis', status: 'succeeded', args: {}, run_id: null }],
      }),
      message({ message_id: 'm-u2', role: 'user', content: '第 4 条是哪个素材？' }),
      message({
        message_id: 'm-a2', content: '第 4 条是《浅蓝羽绒服挂树上》。', run_id: 'r-1',
        tool_calls: [{ tool: 'read_run', status: 'succeeded', args: {}, run_id: 'r-1' }],
      }),
    ]))
    vi.mocked(getRun).mockResolvedValue(runDetail())
    const wrapper = await mountView()
    await flushPromises()

    // 产出那次运行的一轮有卡片，引用它的一轮没有：整页只出现一次候选清单
    expect((wrapper.text().match(/热点 1/g) ?? []).length).toBe(1)
    expect(wrapper.text()).toContain('第 4 条是《浅蓝羽绒服挂树上》。')
  })

  it('会话打不开时给错误提示，不阻塞输入框', async () => {
    vi.mocked(getChatSession).mockRejectedValue(new Error('会话不存在'))
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('会话不存在')
    expect(wrapper.find('textarea').exists()).toBe(true)
  })

  it('消息区是唯一滚动容器，输入卡片不参与滚动', async () => {
    const wrapper = await mountView()

    const messages = wrapper.find('.chat-messages')
    expect(messages.classes()).toContain('overflow-y-auto')
    expect(messages.classes()).toContain('min-h-0')
    expect(messages.classes()).toContain('flex-1')
    // 输入卡片在滚动容器之外，且不压缩
    expect(messages.element.contains(wrapper.find('textarea').element)).toBe(false)
    const composer = wrapper.find('textarea').element.closest('.chat-composer') as HTMLElement
    expect(composer.className).toContain('shrink-0')
  })

  it('贴底时新消息自动跟随到底部', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ message_id: 'm-1', content: '第一条' }),
    ]))
    const wrapper = await mountView()
    const messages = wrapper.find('.chat-messages').element as HTMLElement
    fakeScrollable(messages)

    // 1000 - 700 - 300 = 0 ≤ 40：用户就在底部
    messages.scrollTop = 700
    messages.dispatchEvent(new Event('scroll'))
    await nextTick()

    const store = useChatStore()
    store.messages = [...store.messages, message({ message_id: 'm-2', content: '第二条' })]
    await flushPromises()

    expect(messages.scrollTop).toBe(1000)
  })

  it('用户上翻看历史时，新消息不把他拽回底部', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ message_id: 'm-1', content: '第一条' }),
    ]))
    const wrapper = await mountView()
    const messages = wrapper.find('.chat-messages').element as HTMLElement
    fakeScrollable(messages)

    // 1000 - 0 - 300 = 700 > 40：用户在翻历史
    messages.scrollTop = 0
    messages.dispatchEvent(new Event('scroll'))
    await nextTick()

    const store = useChatStore()
    store.messages = [...store.messages, message({ message_id: 'm-2', content: '第二条' })]
    await flushPromises()

    expect(messages.scrollTop).toBe(0)
  })

  it('流式增量在贴底时也跟随（不是只看条数）', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({ message_id: 'm-1', role: 'user', content: '帮我看看' }),
    ]))
    const wrapper = await mountView()
    const messages = wrapper.find('.chat-messages').element as HTMLElement
    fakeScrollable(messages)
    messages.scrollTop = 690            // 距底 10px，仍算贴底
    messages.dispatchEvent(new Event('scroll'))
    await nextTick()

    const store = useChatStore()
    store.streamText = '正在写…'
    await flushPromises()

    expect(messages.scrollTop).toBe(1000)
  })

  it('运行中显示当前阶段与尝试次数；失败消息给阶段与 trace 回看（S7.21）', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({
        message_id: 'm-f', status: 'failed', error: '分析失败',
        trace_url: 'https://cloud.langfuse.com/trace/t-1',
        progress: {
          stage: 'retrieve', label: '检索', status: 'failed', attempt: 1,
          error_category: 'embedding_error', updated_at: '2026-10-15T08:00:03+00:00',
        },
      }),
      message({
        message_id: 'm-r', status: 'running',
        progress: {
          stage: 'material_sync', label: '素材同步', status: 'running', attempt: 2,
          started_at: '2026-10-15T08:00:00+00:00', updated_at: '2026-10-15T08:00:03+00:00',
        },
      }),
    ]))
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('失败于「检索」')
    expect(wrapper.text()).toContain('向量化失败')
    expect(wrapper.find('a[href="https://cloud.langfuse.com/trace/t-1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('素材同步')
    expect(wrapper.text()).toContain('第 2 次尝试')
  })

  it('阶段跑太久只提示「耗时较长」，不判定失败（S7.21）', async () => {
    vi.mocked(getChatSession).mockResolvedValue(detail([
      message({
        message_id: 'm-r', status: 'running',
        progress: {
          stage: 'queue_wait', label: '排队等待', status: 'running', attempt: 1,
          started_at: '2020-01-01T00:00:00+00:00', updated_at: '2020-01-01T00:00:01+00:00',
        },
      }),
    ]))
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('耗时较长')
    expect(wrapper.text()).toContain('排队等待')
  })

  it('一次选 3 张图：缩略图都在，发送时整组进 multipart（S7.18）', async () => {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    const wrapper = await mountView()

    const input = wrapper.find('input[type="file"]')
    const files = [1, 2, 3].map((index) =>
      new File([new Uint8Array([index])], `图${index}.png`, { type: 'image/png' }))
    Object.defineProperty(input.element, 'files', { value: files, configurable: true })
    await input.trigger('change')
    await flushPromises()

    // 只数 composer 里的缩略图：页面上还有上下文面板（S7.24）的标签，不能混在一起数
    const composer = wrapper.find('textarea').element.closest('.chat-composer') as HTMLElement
    expect(composer.querySelectorAll('.composer-thumb')).toHaveLength(3)
    expect(wrapper.text()).toContain('已选 3 / 9 张')

    await clickButton(wrapper, '发送')

    const payload = vi.mocked(streamMessage).mock.calls[0][1]
    expect((payload.files ?? []).map((file) => file.name)).toEqual(['图1.png', '图2.png', '图3.png'])
  })

  it('在输入区粘贴两张图：收进选中区、不触发发送（P0 验收项）', async () => {
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    const wrapper = await mountView()

    const event = new Event('paste', { bubbles: true, cancelable: true }) as ClipboardEvent
    const items = [1, 2].map((index) => ({
      kind: 'file',
      type: 'image/png',
      getAsFile: () => new File([new Uint8Array([index])], '', { type: 'image/png' }),
    }))
    Object.defineProperty(event, 'clipboardData', { value: { items } })
    wrapper.find('.chat-composer').element.dispatchEvent(event)
    await flushPromises()

    // 粘贴的图进选中区（截图没名字 → 生成可读名），且绝不自动发送
    const composer = wrapper.find('textarea').element.closest('.chat-composer') as HTMLElement
    expect(composer.querySelectorAll('.composer-thumb')).toHaveLength(2)
    expect(composer.querySelector('.composer-thumb img')?.getAttribute('alt')).toBe('粘贴图片-1.png')
    expect(streamMessage).not.toHaveBeenCalled()
  })

  it('composer 是整块圆角卡：无虚线选图区、无底部说明段', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('.chat-composer').exists()).toBe(true)
    expect(wrapper.find('[role="button"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('图片只用于这一次解析')
    expect(wrapper.text()).toContain('最多 9 张')
    expect(wrapper.find('textarea').attributes('placeholder'))
      .toBe('想蹭哪条热点？说一句话，或者直接丢图')
  })

  it('空输入时发送按钮禁用，有内容后可用', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('.composer-send').attributes('disabled')).toBeDefined()
    await wrapper.find('textarea').setValue('一句话')
    expect(wrapper.find('.composer-send').attributes('disabled')).toBeUndefined()
  })

  it('Enter 发送；Shift+Enter 与 IME 组合中的 Enter 不发送', async () => {
    vi.mocked(listChatSessions).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    const wrapper = await mountView()
    const area = wrapper.find('textarea')
    await area.setValue('帮我蹭一下')

    await area.trigger('keydown', { key: 'Enter', shiftKey: true, isComposing: false })
    expect(streamMessage).not.toHaveBeenCalled()
    await area.trigger('keydown', { key: 'Enter', shiftKey: false, isComposing: true })
    expect(streamMessage).not.toHaveBeenCalled()
    await area.trigger('keydown', { key: 'Enter', shiftKey: false, isComposing: false })
    await flushPromises()
    expect(streamMessage).toHaveBeenCalledTimes(1)
  })

  it('确认未受理的失败恢复草稿；SSE 已受理后断流不恢复、不自动重发', async () => {
    const { ApiRequestError } = await import('@/api/client')
    vi.mocked(streamMessage).mockRejectedValueOnce(
      new ApiRequestError('服务不可用', undefined, 503),
    )
    const wrapper = await mountView()
    const area = wrapper.find('textarea')
    await area.setValue('第一条草稿')
    await clickButton(wrapper, '发送')
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('第一条草稿')
    expect(streamMessage).toHaveBeenCalledTimes(1)

    // 流已开始后断流：不恢复草稿，避免重复提交已落库的消息
    vi.mocked(streamMessage).mockImplementationOnce(async (...args) => {
      const onEvent = args[2] as (event: { event: string; data: Record<string, unknown> }) => void
      onEvent({ event: 'turn_started', data: {} })
      throw new Error('stream broken')
    })
    await clickButton(wrapper, '发送')
    await flushPromises()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')
    expect(streamMessage).toHaveBeenCalledTimes(2)
  })

  describe('工具进度折叠', () => {
    function runningMessage(): ChatMessage {
      return message({
        message_id: 'm-r', status: 'running',
        progress: {
          stage: 'retrieve', label: '检索', status: 'running', attempt: 1,
          started_at: '2026-11-14T08:00:00+00:00', updated_at: '2026-11-14T08:00:10+00:00',
        },
      })
    }

    function seedSteps(): void {
      const store = useChatStore()
      store.toolSteps.push(
        { stage: '排队等待', done: 0, total: 100, status: 'succeeded', startedAt: 0, endedAt: 8400 },
        { stage: '检索', done: 0, total: 100, status: 'running', startedAt: 8400, endedAt: null },
      )
    }

    it('运行中默认只显示摘要行，不渲染阶段流水、不出现假进度', async () => {
      vi.mocked(getChatSession).mockResolvedValue(detail([runningMessage()]))
      const wrapper = await mountView()
      // 打开会话会重置上一轮的流水，所以在 init 之后再塞本轮步骤
      seedSteps()
      await nextTick()

      expect(wrapper.text()).toContain('检索')
      expect(wrapper.text()).toContain('详情')
      expect(wrapper.text()).not.toContain('0/100')
      expect(wrapper.findAll('.step-row')).toHaveLength(0)
    })

    it('点开详情才渲染时间线，再点收起回到摘要行', async () => {
      vi.mocked(getChatSession).mockResolvedValue(detail([runningMessage()]))
      const wrapper = await mountView()
      seedSteps()
      await nextTick()

      await clickButton(wrapper, '详情')

      expect(wrapper.findAll('.step-row')).toHaveLength(2)
      expect(wrapper.text()).toContain('排队等待')
      expect(wrapper.text()).toContain('8 秒')
      expect(wrapper.find('button[aria-expanded]').attributes('aria-expanded')).toBe('true')

      await clickButton(wrapper, '收起')
      expect(wrapper.findAll('.step-row')).toHaveLength(0)
    })

    it('没有阶段事件时不显示详情按钮，摘要行退回工具名', async () => {
      vi.mocked(getChatSession).mockResolvedValue(detail([
        message({ message_id: 'm-r', status: 'running' }),
      ]))
      const wrapper = await mountView()
      useChatStore().activeTool = { label: '搜索热点', startedAt: Date.now() }
      await nextTick()

      expect(wrapper.text()).toContain('搜索热点')
      expect(wrapper.text()).not.toContain('详情')
    })

    it('阶段快照已结束时不当摘要主语，退回当前工具名', async () => {
      // 真实场景：supervisor 阶段已 succeeded，工具（改写文案）还在跑且没有阶段键
      vi.mocked(getChatSession).mockResolvedValue(detail([
        message({
          message_id: 'm-r', status: 'running',
          progress: {
            stage: 'supervisor', label: '理解与决策', status: 'succeeded', attempt: 1,
            started_at: '2026-11-14T08:00:00+00:00', updated_at: '2026-11-14T08:00:01+00:00',
          },
        }),
      ]))
      const wrapper = await mountView()
      useChatStore().activeTool = { label: '改写文案', startedAt: Date.now() }
      await nextTick()

      expect(wrapper.text()).toContain('改写文案')
      expect(wrapper.text()).not.toContain('理解与决策')
    })

    it('点开详情后跟随滚到底，用户不会以为没展开', async () => {
      vi.mocked(getChatSession).mockResolvedValue(detail([runningMessage()]))
      const wrapper = await mountView()
      seedSteps()
      await nextTick()

      const messages = wrapper.find('.chat-messages').element as HTMLElement
      fakeScrollable(messages)
      messages.scrollTop = 700          // 1000 - 700 - 300 = 0：用户在底部
      messages.dispatchEvent(new Event('scroll'))
      await nextTick()

      await clickButton(wrapper, '详情')

      expect(messages.scrollTop).toBe(1000)
    })
  })
})
