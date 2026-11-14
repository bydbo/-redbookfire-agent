import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import StepTimeline from '@/components/StepTimeline.vue'
import type { ToolStep } from '@/stores/chat'

function step(overrides: Partial<ToolStep> = {}): ToolStep {
  return {
    stage: '排队等待',
    done: 0,
    total: 0,
    status: 'succeeded',
    startedAt: 0,
    endedAt: 0,
    ...overrides,
  }
}

describe('StepTimeline（工具阶段时间线）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-14T08:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('已完成的阶段：打勾并按 endedAt - startedAt 显示耗时', () => {
    const wrapper = mount(StepTimeline, {
      props: { steps: [step({ stage: '排队等待', startedAt: 0, endedAt: 8400 })] },
    })

    expect(wrapper.find('.step-row').classes()).toContain('is-done')
    expect(wrapper.text()).toContain('排队等待')
    expect(wrapper.text()).toContain('8 秒')
  })

  it('当前阶段：高亮并显示「进行中 · 已用时长」', () => {
    const now = Date.now()
    const wrapper = mount(StepTimeline, {
      props: {
        steps: [step({ stage: '检索', status: 'running', startedAt: now - 3000, endedAt: null })],
      },
    })

    expect(wrapper.find('.step-row').classes()).toContain('is-active')
    expect(wrapper.text()).toContain('进行中 · 3 秒')
  })

  it('当前阶段每秒重算一次已用时长', async () => {
    const now = Date.now()
    const wrapper = mount(StepTimeline, {
      props: { steps: [step({ stage: '检索', status: 'running', startedAt: now, endedAt: null })] },
    })

    expect(wrapper.text()).toContain('进行中 · 0 秒')
    vi.advanceTimersByTime(2000)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('进行中 · 2 秒')
  })

  it('失败的阶段显示未完成，不冒充成功', () => {
    const wrapper = mount(StepTimeline, {
      props: { steps: [step({ stage: '素材同步', status: 'failed', startedAt: 0, endedAt: 1200 })] },
    })

    expect(wrapper.find('.step-row').classes()).toContain('is-failed')
    expect(wrapper.text()).toContain('未完成')
  })
})
