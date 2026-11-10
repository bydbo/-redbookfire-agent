import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import App from '@/App.vue'
import { useAppStore } from '@/stores/app'

// AppSidebar 挂载会拉会话列表；保留其它真实导出，只 mock 会打网络的几个。
vi.mock('@/api/chat', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/chat')>()
  return {
    ...actual,
    listChatSessions: vi.fn(async () => ({ items: [], total: 0 })),
    getChatSession: vi.fn(async (sessionId: string) => ({
      session_id: sessionId, title: '', created_at: '', updated_at: '', messages: [],
    })),
    getChatContext: vi.fn(async () => null),
    createChatSession: vi.fn(async () => ({
      session_id: 's-new', title: '', created_at: '', updated_at: '',
    })),
    deleteChatSession: vi.fn(async () => undefined),
    renameChatSession: vi.fn(async (sessionId: string, title: string) => ({
      session_id: sessionId, title, created_at: '', updated_at: '',
    })),
  }
})

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'chat', component: { template: '<div>对话占位</div>' } },
      { path: '/analyze', name: 'analysis', component: { template: '<div>分析台占位</div>' } },
      { path: '/materials', name: 'materials', component: { template: '<div>素材库占位</div>' } },
      { path: '/runs', name: 'run-history', component: { template: '<div>历史占位</div>' } },
      { path: '/runs/:runId', name: 'run-result', component: { template: '<div>详情占位</div>' } },
    ],
  })
}

async function mountApp(path = '/') {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const wrapper = mount(App, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('App 左侧导航壳层（S7.30）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.localStorage.clear()
    document.documentElement.dataset.theme = 'light'
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('侧栏只提供三项导航，点击第三项跳运行历史', async () => {
    const { wrapper, router } = await mountApp()
    const links = wrapper.findAll('.app-nav-link')
    expect(links).toHaveLength(3)
    expect(wrapper.findAll('.app-nav-link .nav-label').map((node) => node.text()))
      .toEqual(['对话', '素材库', '运行历史'])

    await links[2].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('run-history')
  })

  it('点击素材库跳 /materials（导航与路由一致）', async () => {
    const { wrapper, router } = await mountApp()
    await wrapper.findAll('.app-nav-link')[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/materials')
  })

  it('/analyze 是隐藏页：直链可开、导航无高亮', async () => {
    const { wrapper, router } = await mountApp('/analyze')
    expect(router.currentRoute.value.name).toBe('analysis')
    expect(wrapper.text()).toContain('分析台占位')
    expect(wrapper.findAll('.app-nav-link.is-active')).toHaveLength(0)
  })

  it('结果详情页把「运行历史」标记为当前栏', async () => {
    const { wrapper } = await mountApp('/runs/8860c661-bccb-4246-ba8b-1da6bb63f5de')
    const active = wrapper.findAll('.app-nav-link.is-active')
    expect(active).toHaveLength(1)
    expect(active[0].find('.nav-label').text()).toBe('运行历史')
    expect(active[0].attributes('aria-current')).toBe('page')
  })

  it('外壳锁视口、内容区自己滚，且顶层有 n-message-provider', async () => {
    const { wrapper } = await mountApp()
    const content = wrapper.find('.app-content')
    expect(content.classes()).toContain('min-h-0')
    expect(content.classes()).toContain('flex-1')
    expect(content.classes()).toContain('overflow-y-auto')
    expect(wrapper.find('.app-shell').classes()).toContain('h-dvh')
    // 对话页由消息区内滚，外层在 md 以上关闭滚动
    expect(content.classes()).toContain('app-content--chat')
    expect(wrapper.findComponent(NMessageProvider).exists()).toBe(true)
  })

  it('侧栏常驻「历史会话」区块，底部主题切换可见', async () => {
    const { wrapper } = await mountApp()
    expect(wrapper.text()).toContain('历史会话')
    expect(wrapper.text()).toContain('新会话')
    expect(wrapper.find('.theme-toggle').exists()).toBe(true)
  })

  it('暗色开关翻转 html[data-theme] 并写入 localStorage', async () => {
    const { wrapper } = await mountApp()
    const toggle = wrapper.findAll('button').find((button) => button.text().includes('暗色'))
    expect(toggle).toBeDefined()

    await toggle?.trigger('click')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem('xhs-theme')).toBe('dark')
    expect(wrapper.text()).toContain('浅色')

    await wrapper.findAll('button').find((button) => button.text().includes('浅色'))?.trigger('click')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(window.localStorage.getItem('xhs-theme')).toBe('light')
  })

  it('点击侧栏会话项后跳到对话页', async () => {
    const { listChatSessions } = await import('@/api/chat')
    vi.mocked(listChatSessions).mockResolvedValueOnce({
      items: [{
        session_id: 's-1', title: '一条历史会话', created_at: '', updated_at: '',
        message_count: 1, last_message_preview: '预览',
      }],
      total: 1,
    })
    const { wrapper, router } = await mountApp('/materials')
    await flushPromises()

    const item = wrapper.find('.session-main')
    expect(item.exists()).toBe(true)
    await item.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('chat')
  })
})

describe('主题初始值', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.unstubAllGlobals()
  })

  it('localStorage 里存过就按存的来（手动选择优先于系统）', () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
    window.localStorage.setItem('xhs-theme', 'dark')
    setActivePinia(createPinia())

    const store = useAppStore()

    expect(store.isDark).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(store.palette.surface).toBe('#19191e')
  })

  it('没存过就跟随系统 prefers-color-scheme', () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
    setActivePinia(createPinia())

    const store = useAppStore()

    expect(store.isDark).toBe(true)
    expect(store.palette.surface).toBe('#19191e')
  })
})
