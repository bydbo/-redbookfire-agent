import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { MaterialItem, MaterialList, TrashList } from '@/api/materials'
import { listMaterials, listTrash, scanMaterials } from '@/api/materials'
import { useMaterialsStore } from '@/stores/materials'
import MaterialsView from '@/views/MaterialsView.vue'

vi.mock('@/api/materials', () => ({
  listMaterials: vi.fn(),
  listTrash: vi.fn(),
  scanMaterials: vi.fn(),
  getMaterialTask: vi.fn(),
  uploadMaterial: vi.fn(),
  trashMaterial: vi.fn(),
  restoreTrash: vi.fn(),
  purgeTrash: vi.fn(),
  updateMaterial: vi.fn(),
  getMaterial: vi.fn(),
}))

function item(id: string, overrides: Partial<MaterialItem> = {}): MaterialItem {
  return {
    id,
    path: `D:/materials/运动/${id}.mp4`,
    type: 'video',
    title: `素材 ${id}`,
    description: '',
    tags: ['羽毛球'],
    duration_s: 12,
    width: 1920,
    height: 1080,
    has_audio: true,
    size_bytes: 2048,
    source: 'sidecar',
    keyframes: [],
    indexed_at: '2026-10-06T04:00:00+00:00',
    dir: '运动',
    ...overrides,
  }
}

function listing(): MaterialList {
  return {
    items: [item('a'), item('b', { tags: [], keyframes: ['runs/_index/keyframes/b/kf_0.jpg'] })],
    total: 2,
    limit: 24,
    offset: 0,
    dirs: [{ path: '运动', count: 2 }],
  }
}

const EMPTY_TRASH: TrashList = { items: [], total: 0 }

async function mountView() {
  const wrapper = mount(MaterialsView)
  await flushPromises()
  return wrapper
}

describe('MaterialsView（S6.5）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.localStorage.clear()
    vi.mocked(listMaterials).mockReset().mockResolvedValue(listing())
    vi.mocked(listTrash).mockReset().mockResolvedValue(EMPTY_TRASH)
    vi.mocked(scanMaterials).mockReset().mockResolvedValue({ task_id: 'task-1' })
  })

  it('渲染工具条、上传区与网格卡片', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('素材库')
    expect(wrapper.text()).toContain('素材 a')
    expect(wrapper.text()).toContain('素材 b')
    expect(wrapper.text()).toContain('羽毛球')
    expect(wrapper.text()).toContain('无标签')          // b 没有标签
    expect(wrapper.text()).toContain('视频')
    expect(wrapper.text()).toContain('人工说明')
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('把素材拖进来')
  })

  it('空库给出可照做的提示', async () => {
    vi.mocked(listMaterials).mockResolvedValue(
      { items: [], total: 0, limit: 24, offset: 0, dirs: [] })
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('这里还没有素材')
    expect(wrapper.text()).toContain('扫描')
  })

  it('网格 / 列表切换会改 store 并落 localStorage', async () => {
    const wrapper = await mountView()
    const store = useMaterialsStore()

    const listButton = wrapper.findAll('button').find((button) => button.text() === '列表')
    await listButton?.trigger('click')

    expect(store.viewMode).toBe('list')
    expect(window.localStorage.getItem('xhs-materials-view')).toBe('list')
    expect(wrapper.text()).toContain('素材 a')          // 两种视图都列出素材
  })

  it('搜索框回车把关键词交给 store（回第一页）', async () => {
    const wrapper = await mountView()
    const store = useMaterialsStore()
    const search = wrapper.find('input[placeholder="搜索标题或标签"]')

    await search.setValue('羽毛球')
    await search.trigger('keyup.enter')
    await flushPromises()

    expect(store.filters.q).toBe('羽毛球')
    expect(listMaterials).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: '羽毛球', offset: 0 }))
  })

  it('扫描按钮走 store.scan()（投递索引任务）', async () => {
    const wrapper = await mountView()
    const scan = wrapper.findAll('button').find((button) => button.text() === '扫描')

    await scan?.trigger('click')
    await flushPromises()

    expect(scanMaterials).toHaveBeenCalledTimes(1)
  })

  it('回收站抽屉列出内容并提供恢复 / 真删', async () => {
    vi.mocked(listTrash).mockResolvedValue({
      items: [{ path: '运动/旧片.mp4', name: '旧片.mp4', type: 'video', size_bytes: 1024,
                mtime: '2026-10-06T04:00:00+00:00' }],
      total: 1,
    })
    const wrapper = await mountView()

    const trashButton = wrapper.findAll('button').find((button) => button.text() === '回收站')
    await trashButton?.trigger('click')
    await flushPromises()

    expect(listTrash).toHaveBeenCalledTimes(1)
    const store = useMaterialsStore()
    expect(store.trashOpen).toBe(true)
    expect(store.trash[0].name).toBe('旧片.mp4')

    // 抽屉内容用 teleport 渲染到 body：从 document 上断言语义（wrapper 里看不到）
    const body = document.body.textContent ?? ''
    expect(body).toContain('旧片.mp4')
    expect(body).toContain('恢复')
    expect(body).toContain('真删')
    expect(wrapper.findAll('button').length).toBeGreaterThan(0)
  })

  it('上传区支持拖拽（走 store.uploadFiles）', async () => {
    const wrapper = await mountView()
    const store = useMaterialsStore()
    const spy = vi.spyOn(store, 'uploadFiles').mockResolvedValue()
    const zone = wrapper.find('.border-dashed')
    const file = new File([new Uint8Array([1])], '新片.mp4', { type: 'video/mp4' })

    await zone.trigger('drop', { dataTransfer: { files: [file] } })

    expect(spy).toHaveBeenCalledWith([file])
  })
})
