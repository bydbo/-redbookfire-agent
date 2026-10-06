import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RunDetail } from '@/api/analysis'
import { getRun } from '@/api/analysis'
import type { ChatMessage, ChatSessionDetail, ChatSessionList } from '@/api/chat'
import { createChatSession, getChatSession, listChatSessions, streamMessage } from '@/api/chat'
import ChatView from '@/views/ChatView.vue'

vi.mock('@/api/chat', () => ({
  createChatSession: vi.fn(),
  listChatSessions: vi.fn(),
  getChatSession: vi.fn(),
  streamMessage: vi.fn(),
  chatAttachmentUrl: (session: string, message: string, index: number) =>
    `/api/chat/sessions/${session}/attachments/${message}/${index}`,
}))

vi.mock('@/api/analysis', () => ({
  getRun: vi.fn(),
  getKeyframeUrl: (id: string, index: number) => `/api/materials/${id}/keyframes/${index}`,
  ELEMENT_TYPE_LABELS: {
    ip: 'IP', topic: '主题', scene: '场景', visual: '画面', emotion: '情绪',
    sound: '声音', conflict: '冲突', format: '形式', audience: '人群',
  },
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
    hotspots: [{
      hotspot_id: 'h-1',
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
  const wrapper = mount(ChatView)
  await flushPromises()
  return wrapper
}

async function clickButton(wrapper: ReturnType<typeof mount>, label: string): Promise<void> {
  const button = wrapper.findAll('button').find((item) => item.text() === label)
  expect(button, `没有找到按钮：${label}`).toBeDefined()
  await button?.trigger('click')
  await flushPromises()
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

  it('空态给引导，历史会话列在旁边', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('还没有消息')
    expect(wrapper.text()).toContain('帮我蹭一下这个热点')
    expect(wrapper.findAll('.chat-session-item')).toHaveLength(1)
    // 刷新恢复：最近一条会话自动打开（消息从库里读回来）
    expect(getChatSession).toHaveBeenCalledWith('s-1')
    expect(wrapper.find('.chat-session-item').classes()).toContain('is-active')
  })

  it('新对话 + 发送一句话：先建会话，再流式，最后按库里消息渲染', async () => {
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

    await clickButton(wrapper, '新对话')
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
      message({ message_id: 'm-a', content: '跑完了。', run_id: 'r-1' }),
    ]))
    vi.mocked(getRun).mockResolvedValue(runDetail())
    const wrapper = await mountView()

    await wrapper.find('.chat-session-item').trigger('click')   // 打开历史会话
    await flushPromises()
    expect(getRun).toHaveBeenCalledWith('r-1')
    expect(wrapper.text()).toContain('爆点要素')
    expect(wrapper.text()).toContain('正文正文')
    expect(wrapper.text()).toContain('文案初稿')
  })

  it('会话打不开时给错误提示，不阻塞输入框', async () => {
    vi.mocked(getChatSession).mockRejectedValue(new Error('会话不存在'))
    const wrapper = await mountView()

    await wrapper.find('.chat-session-item').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('会话不存在')
    expect(wrapper.find('textarea').exists()).toBe(true)
  })
})
