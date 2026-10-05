import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import App from '@/App.vue'
import { useAppStore } from '@/stores/app'

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'analysis', component: { template: '<div>分析台占位</div>' } },
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
  return { wrapper, router }
}

describe('App 壳层（S5.7 顶栏 + 主题）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.localStorage.clear()
    document.documentElement.dataset.theme = 'light'
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('顶栏提供分析台与运行历史两个入口', async () => {
    const { wrapper, router } = await mountApp()
    const links = wrapper.findAll('.app-nav-link').map((link) => link.text())
    expect(links).toEqual(['分析台', '运行历史'])

    await wrapper.findAll('.app-nav-link')[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('run-history')
  })

  it('结果详情页把「运行历史」标记为当前栏', async () => {
    const { wrapper } = await mountApp('/runs/8860c661-bccb-4246-ba8b-1da6bb63f5de')
    const active = wrapper.findAll('.app-nav-link').filter((link) => link.classes('is-active'))
    expect(active).toHaveLength(1)
    expect(active[0].text()).toBe('运行历史')
  })

  it('暗色开关翻转 html[data-theme] 并写入 localStorage', async () => {
    const { wrapper } = await mountApp()
    const toggle = wrapper.findAll('button').find((button) => button.text() === '暗色')
    expect(toggle).toBeDefined()

    await toggle?.trigger('click')

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem('xhs-theme')).toBe('dark')
    expect(wrapper.text()).toContain('浅色')       // 按钮切成反向动作

    await wrapper.findAll('button').find((button) => button.text() === '浅色')?.trigger('click')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(window.localStorage.getItem('xhs-theme')).toBe('light')
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
