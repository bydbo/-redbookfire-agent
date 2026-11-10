import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { ChatSessionSummary } from '@/api/chat'
import {
  createChatSession, deleteChatSession, getChatSession, listChatSessions, renameChatSession,
} from '@/api/chat'
import AppSidebar from '@/components/AppSidebar.vue'
import { useChatStore } from '@/stores/chat'

vi.mock('@/api/chat', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/chat')>()
  return {
    ...actual,
    listChatSessions: vi.fn(),
    getChatSession: vi.fn(),
    getChatContext: vi.fn(async () => null),
    createChatSession: vi.fn(),
    deleteChatSession: vi.fn(),
    renameChatSession: vi.fn(),
    streamMessage: vi.fn(async () => undefined),
    chatAttachmentUrl: () => '',
  }
})

function item(id: string, title: string): ChatSessionSummary {
  return {
    session_id: id, title, created_at: '', updated_at: '',
    message_count: 1, last_message_preview: '预览',
  }
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'chat', component: { template: '<div>对话</div>' } },
      { path: '/materials', name: 'materials', component: { template: '<div>素材库</div>' } },
      { path: '/runs', name: 'run-history', component: { template: '<div>历史</div>' } },
      { path: '/runs/:runId', name: 'run-result', component: { template: '<div>详情</div>' } },
    ],
  })
}

async function mountSidebar(path = '/') {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(AppSidebar) },
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('AppSidebar（S7.30 左侧导航与会话列表）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(listChatSessions).mockReset()
    vi.mocked(getChatSession).mockReset()
    vi.mocked(createChatSession).mockReset()
    vi.mocked(deleteChatSession).mockReset()
    vi.mocked(renameChatSession).mockReset()
    vi.mocked(listChatSessions).mockResolvedValue({
      items: [item('s-1', '会话一'), item('s-2', '会话二')], total: 2,
    })
    vi.mocked(getChatSession).mockResolvedValue({
      session_id: 's-2', title: '会话二', created_at: '', updated_at: '', messages: [],
    })
    vi.mocked(createChatSession).mockResolvedValue({
      session_id: 's-9', title: '', created_at: '', updated_at: '',
    })
    vi.mocked(deleteChatSession).mockResolvedValue(undefined)
    vi.mocked(renameChatSession).mockImplementation(async (id: string, title: string) => ({
      session_id: id, title, created_at: '', updated_at: '',
    }))
  })

  afterEach(() => {
    document.querySelectorAll('.n-message').forEach((node) => node.remove())
  })

  it('三项导航、主色新会话按钮与激活态（无竖条类）', async () => {
    const { wrapper } = await mountSidebar()
    expect(wrapper.findAll('.nav-label').map((node) => node.text()))
      .toEqual(['对话', '素材库', '运行历史'])
    expect(wrapper.find('.n-button--primary-type').exists()).toBe(true)
    const active = wrapper.findAll('.app-nav-link.is-active')
    expect(active).toHaveLength(1)
    expect(active[0].attributes('aria-current')).toBe('page')
    expect(active[0].classes()).not.toContain('with-bar')
  })

  it('结果详情路由下高亮「运行历史」', async () => {
    const { wrapper } = await mountSidebar('/runs/r-1')
    const active = wrapper.findAll('.app-nav-link.is-active')
    expect(active).toHaveLength(1)
    expect(active[0].find('.nav-label').text()).toBe('运行历史')
  })

  it('挂载只拉会话列表，不会自动打开最近会话（不调 init）', async () => {
    const { wrapper } = await mountSidebar()
    expect(listChatSessions).toHaveBeenCalled()
    expect(getChatSession).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('历史会话')
  })

  it('点击「新会话」创建会话并跳到对话页', async () => {
    const { wrapper, router } = await mountSidebar('/materials')
    await wrapper.find('.n-button--primary-type').trigger('click')
    await flushPromises()
    expect(createChatSession).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('chat')
  })

  it('创建期间按钮禁用，重复点击不重复创建', async () => {
    const deferred: { finish: () => void } = { finish: () => {} }
    vi.mocked(createChatSession).mockImplementationOnce(() => new Promise((resolve) => {
      deferred.finish = () => resolve({
        session_id: 's-9', title: '', created_at: '', updated_at: '',
      })
    }))
    const { wrapper } = await mountSidebar()
    const button = wrapper.find('.n-button--primary-type')
    await button.trigger('click')
    await button.trigger('click')
    expect(createChatSession).toHaveBeenCalledTimes(1)
    deferred.finish()
    await flushPromises()
  })

  it('会话列表渲染两项，当前项高亮', async () => {
    useChatStore().currentId = 's-2'
    const { wrapper } = await mountSidebar()
    const rows = wrapper.findAll('.chat-session-item')
    expect(rows).toHaveLength(2)
    expect(rows[1].classes()).toContain('is-active')
  })

  it('点击会话项打开会话并跳到 /', async () => {
    const { wrapper, router } = await mountSidebar('/materials')
    await wrapper.findAll('.session-main')[1].trigger('click')
    await flushPromises()
    expect(getChatSession).toHaveBeenCalledWith('s-2')
    expect(router.currentRoute.value.name).toBe('chat')
  })

  it('删除图标只删对应会话、不触发会话切换，并强制刷新列表', async () => {
    const { wrapper } = await mountSidebar()
    const before = vi.mocked(listChatSessions).mock.calls.length
    await wrapper.findAll('.session-delete')[1].trigger('click')
    await flushPromises()
    expect(deleteChatSession).toHaveBeenCalledWith('s-2')
    expect(getChatSession).not.toHaveBeenCalled()
    expect(vi.mocked(listChatSessions).mock.calls.length).toBeGreaterThan(before)
  })

  it('删除返回 204 空响应按成功处理，失败给 toast', async () => {
    const { wrapper } = await mountSidebar()
    await wrapper.findAll('.session-delete')[0].trigger('click')
    await flushPromises()
    expect(deleteChatSession).toHaveBeenCalledTimes(1)

    vi.mocked(deleteChatSession).mockRejectedValueOnce(new Error('删除失败'))
    await wrapper.findAll('.session-delete')[1].trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('删除失败')
  })

  it('行内重命名：Enter 调 PATCH 并原地更新列表标题', async () => {
    const { wrapper } = await mountSidebar()
    await wrapper.findAll('.session-rename')[0].trigger('click')
    await flushPromises()
    const input = wrapper.find('.session-rename-input input')
    expect(input.exists()).toBe(true)
    expect((input.element as HTMLInputElement).value).toBe('会话一')
    await input.setValue('改过的名字')
    await input.trigger('keydown', { key: 'Enter', isComposing: false })
    await flushPromises()
    expect(renameChatSession).toHaveBeenCalledWith('s-1', '改过的名字')
    expect(wrapper.findAll('.session-main')[0].text()).toContain('改过的名字')
  })

  it('重命名：Esc 取消后 blur 零请求；空标题拒绝且保持编辑态', async () => {
    const { wrapper } = await mountSidebar()
    await wrapper.findAll('.session-rename')[0].trigger('click')
    await flushPromises()
    await wrapper.find('.session-rename-input input').setValue('新名')
    await wrapper.find('.session-rename-input input')
      .trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(renameChatSession).not.toHaveBeenCalled()
    expect(wrapper.find('.session-rename-input').exists()).toBe(false)

    await wrapper.findAll('.session-rename')[0].trigger('click')
    await flushPromises()
    await wrapper.find('.session-rename-input input').setValue('   ')
    await wrapper.find('.session-rename-input input')
      .trigger('keydown', { key: 'Enter', isComposing: false })
    await flushPromises()
    expect(renameChatSession).not.toHaveBeenCalled()
    expect(wrapper.find('.session-rename-input').exists()).toBe(true)
  })

  it('重命名：新旧标题相同直接退出且零请求；编辑中隐藏删除图标', async () => {
    const { wrapper } = await mountSidebar()
    await wrapper.findAll('.session-rename')[0].trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.session-delete')).toHaveLength(1)  // 只渲染非编辑行
    await wrapper.find('.session-rename-input input')
      .trigger('keydown', { key: 'Enter', isComposing: false })
    await flushPromises()
    expect(renameChatSession).not.toHaveBeenCalled()
    expect(wrapper.find('.session-rename-input').exists()).toBe(false)
  })

  it('重命名：IME 组合中的 Enter 不提交', async () => {
    const { wrapper } = await mountSidebar()
    await wrapper.findAll('.session-rename')[0].trigger('click')
    await flushPromises()
    await wrapper.find('.session-rename-input input').setValue('改名')
    await wrapper.find('.session-rename-input input')
      .trigger('keydown', { key: 'Enter', isComposing: true })
    await flushPromises()
    expect(renameChatSession).not.toHaveBeenCalled()
  })

  it('删除 / 重命名图标的 aria-label 固定', async () => {
    const { wrapper } = await mountSidebar()
    expect(wrapper.findAll('.session-delete')[0].attributes('aria-label')).toBe('删除对话')
    expect(wrapper.findAll('.session-rename')[0].attributes('aria-label')).toBe('重命名')
  })

  it('列表加载失败时在侧栏内给可读提示（不弹全屏）', async () => {
    vi.mocked(listChatSessions).mockRejectedValueOnce(new Error('网络不可用'))
    const { wrapper } = await mountSidebar()
    expect(wrapper.text()).toContain('网络不可用')
    expect(wrapper.find('.n-alert').exists()).toBe(false)
  })

  it('drawer 变体不写固定宽度类，点击会话会 emit navigate', async () => {
    const router = makeRouter()
    await router.push('/materials')
    await router.isReady()
    let navigated = false
    const wrapper = mount(NMessageProvider, {
      slots: {
        default: () => h(AppSidebar, {
          variant: 'drawer',
          onNavigate: () => { navigated = true },
        }),
      },
      global: { plugins: [router] },
    })
    await flushPromises()
    expect(wrapper.find('.app-sidebar-inner').classes()).not.toContain('md:w-60')
    await wrapper.findAll('.session-main')[0].trigger('click')
    await flushPromises()
    expect(navigated).toBe(true)
  })
})
