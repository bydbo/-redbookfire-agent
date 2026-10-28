import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { HotspotResult as HotspotResultData } from '@/api/analysis'
import HotspotResult from '@/components/HotspotResult.vue'

function candidate(overrides: Record<string, unknown> = {}) {
  return {
    rank: 1,
    material_id: 'm-1',
    score: 0.8,
    recall_sources: ['literal'] as ('literal' | 'vector')[],
    hits: [],
    missing: [],
    reasons: ['命中原素材标题'],
    usage: '放开头',
    material: {
      original_material_id: null,
      id: 'm-1', path: '运动/旧片.mp4', type: 'video' as const, title: '历史标题',
      description: '历史描述', tags: ['旧标签'], elements: [], duration_s: 12,
      width: 1080, height: 1920, has_audio: true, size_bytes: 10, mtime: null,
      source: 'sidecar' as const, keyframes: ['runs/_index/keyframes/old.jpg'],
      indexed_at: null, fingerprint: 'fp', embedding_model: null,
    },
    current_material: {
      exists: true, file_available: true, preview_available: true,
      reason: null, path: '运动/旧片.mp4', title: '历史标题',
    },
    evidence: {},
    ...overrides,
  }
}

function hotspot(overrides: Partial<HotspotResultData> = {}): HotspotResultData {
  return {
    position: 1,
    hotspot_id: 'h-1',
    hotspot_raw: '热点',
    status: 'succeeded',
    error: null,
    clue: {
      why_it_works: ['反差'], mechanisms: [{ name: '反差' }],
      elements: [{ type: 'topic', value: '夜跑', weight: 0.9, confidence: 0.9 }],
    },
    coverage: { ratio: 1, covered: [], gaps: [] },
    retrieval_meta: {},
    gap_advice: null,
    candidates: [candidate()],
    draft: null,
    evidence: {},
    ...overrides,
  } as HotspotResultData
}

describe('HotspotResult（E8 历史依据）', () => {
  it('素材标签与保存的命中要素分开，并显示命中依据而不重算旧结果', async () => {
    const historical = hotspot({ candidates: [candidate({
      score: 0.908,
      material: { ...candidate().material, title: '多人划桨板游湖', tags: ['桨板', '户外'] },
      hits: [{ element_type: 'topic', clue_value: '羽毛球', hit_value: '关键词「户外」命中',
        similarity: 0.9, contribution: 0.4 }],
    })] })
    const original = JSON.stringify(historical)
    const wrapper = mount(HotspotResult, {
      props: { hotspot: historical, runId: 'r-1', position: 1 },
    })
    const materialTags = wrapper.get('[data-testid="material-tags"]')
    const matched = wrapper.get('[data-testid="matched-elements"]')
    expect(materialTags.text()).toContain('素材标签')
    expect(materialTags.text()).toContain('桨板')
    expect(materialTags.text()).not.toContain('羽毛球')
    expect(matched.text()).toContain('命中热点要素')
    expect(matched.text()).toContain('主题 · 羽毛球')
    expect(matched.text()).toContain('依据：关键词「户外」命中')
    expect(wrapper.text()).toContain('91 分')
    expect(JSON.stringify(historical)).toBe(original)

    await wrapper.setProps({ hotspot: hotspot({ candidates: [candidate({
      material: { ...candidate().material, tags: ['夜跑'] },
      hits: [{ element_type: 'topic', clue_value: '跑步', hit_value: '同类型词典「夜跑」命中',
        similarity: 0.9, contribution: 0.4 }],
    })] }) })
    expect(wrapper.get('[data-testid="material-tags"]').text()).toContain('夜跑')
    expect(wrapper.get('[data-testid="matched-elements"]').text()).toContain('主题 · 跑步')
    expect(wrapper.get('[data-testid="matched-elements"]').text()).not.toContain('羽毛球')
  })

  it('标签或命中依据缺失时明确显示未知', () => {
    const wrapper = mount(HotspotResult, {
      props: { hotspot: hotspot({ candidates: [candidate({
        material: null,
        hits: [{ element_type: 'topic', clue_value: '跑步', hit_value: '',
          similarity: 0.9, contribution: 0.4 }],
      })] }), runId: 'r-1', position: 1 },
    })
    expect(wrapper.get('[data-testid="material-tags"]').text()).toContain('未记录素材标签')
    expect(wrapper.get('[data-testid="matched-elements"]').text()).toContain('未记录命中依据')
  })

  it('删除后保留历史文字，标记当前不可用，不请求当前素材预览', () => {
    const wrapper = mount(HotspotResult, {
      props: {
        hotspot: hotspot({
          candidates: [candidate({
            current_material: {
              exists: false, file_available: null, preview_available: false,
              reason: 'material_deleted', path: null, title: null,
            },
          })],
        }),
        runId: 'r-1',
        position: 1,
      },
    })
    expect(wrapper.text()).toContain('历史标题')
    expect(wrapper.text()).toContain('原素材已删除')
    expect(wrapper.find('img').exists()).toBe(false)
  })

  it('素材摘要缺失时明确提示，不渲染空标题或缩略图', () => {
    const wrapper = mount(HotspotResult, {
      props: {
        hotspot: hotspot({
          candidates: [candidate({ material: null, current_material: {
            exists: false, file_available: null, preview_available: false,
            reason: 'material_deleted', path: null, title: null,
          } })],
        }),
        runId: 'r-1',
        position: 1,
      },
    })
    expect(wrapper.text()).toContain('历史素材摘要缺失')
    expect(wrapper.find('img').exists()).toBe(false)
  })

  it('线索缺失和热点失败分别显示，不冒充空结果', () => {
    const wrapper = mount(HotspotResult, {
      props: {
        hotspot: hotspot({
          clue: null, status: 'failed', error: '模型超时', candidates: [],
          evidence: {
            candidate_integrity: {
              schema_version: 1, origin: 'legacy_run_record', captured_at: '2026-10-01T00:00:00+00:00',
              source_ref: 'run:r-1/p:1', digest: '', completeness: 'partial',
              missing_fields: ['deleted_candidates_unknown'],
            },
            clue: {
              schema_version: 1, origin: 'missing', captured_at: '2026-10-01T00:00:00+00:00',
              source_ref: 'run:r-1/p:1', digest: '', completeness: 'missing',
              missing_fields: ['clue'],
            },
          },
        }),
        runId: 'r-1',
        position: 1,
      },
    })
    expect(wrapper.text()).toContain('历史线索缺失')
    expect(wrapper.text()).toContain('模型超时')
    expect(wrapper.text()).toContain('旧候选完整性无法确认')
  })

  it.each([
    ['complete', 'runtime', 'succeeded', '没有命中的素材'],
    ['partial', 'legacy_run_record', 'succeeded', '旧候选完整性无法确认'],
    [null, null, 'failed', '本次检索未完成或失败'],
  ])('按候选完整性区分合法空结果、旧数据缺口和本次失败：%s', (completeness, origin, status, note) => {
    const wrapper = mount(HotspotResult, {
      props: {
        hotspot: hotspot({
          candidates: [], status: status as HotspotResultData['status'],
          evidence: completeness ? {
            candidate_integrity: {
              schema_version: 1, origin, captured_at: '2026-10-01T00:00:00+00:00',
              source_ref: 'run:r-1/p:1', digest: '', completeness, missing_fields: [],
            },
          } as HotspotResultData['evidence'] : {},
        }), runId: 'r-1', position: 1,
      },
    })
    expect(wrapper.text()).toContain(note)
    if (status === 'failed') expect(wrapper.text()).not.toContain('旧候选完整性无法确认')
  })

  it('缺失覆盖结果显示未知，不画 0% 或声称无缺口', () => {
    const wrapper = mount(HotspotResult, {
      props: { hotspot: hotspot({ coverage: null, status: 'failed', candidates: [] }),
        runId: 'r-1', position: 1 },
    })
    expect(wrapper.text()).toContain('覆盖情况未知')
    expect(wrapper.text()).toContain('本次检索未完成或失败')
    expect(wrapper.text()).not.toContain('无缺口，素材全要素覆盖')
    expect(wrapper.text()).not.toContain('要素覆盖率 0%')
  })
})
