import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { MaterialItem, MaterialTask } from '@/api/materials'
import {
  getMaterialTask,
  listMaterials,
  listTrash,
  purgeTrash,
  restoreTrash,
  scanMaterials,
  trashMaterial,
  updateMaterial,
  uploadMaterial,
} from '@/api/materials'
import { ApiRequestError } from '@/api/client'
import { PAGE_SIZE, useMaterialsStore } from '@/stores/materials'

vi.mock('@/api/materials', () => ({
  listMaterials: vi.fn(),
  getMaterialTask: vi.fn(),
  scanMaterials: vi.fn(),
  uploadMaterial: vi.fn(),
  trashMaterial: vi.fn(),
  listTrash: vi.fn(),
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

function listing(ids: string[], total = ids.length) {
  return {
    items: ids.map((id) => item(id)),
    total,
    limit: PAGE_SIZE,
    offset: 0,
    dirs: [{ path: '运动', count: total }],
  }
}

function task(state: MaterialTask['state'], extra: Partial<MaterialTask> = {}): MaterialTask {
  return { task_id: 'task-1', state, step: null, summary: null, result: null, ...extra }
}

describe('useMaterialsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.localStorage.clear()
    vi.mocked(listMaterials).mockReset().mockResolvedValue(listing(['a', 'b']))
    vi.mocked(getMaterialTask).mockReset().mockResolvedValue(task('SUCCESS', { summary: '好了' }))
    vi.mocked(scanMaterials).mockReset().mockResolvedValue({ task_id: 'task-1' })
    vi.mocked(trashMaterial).mockReset().mockResolvedValue({
      material_id: 'a', path: '运动/a.mp4', trashed_path: '运动/a.mp4', file_missing: false,
    })
    vi.mocked(listTrash).mockReset().mockResolvedValue({ items: [], total: 0 })
    vi.mocked(restoreTrash).mockReset().mockResolvedValue({ task_id: 'task-1' })
    vi.mocked(purgeTrash).mockReset().mockResolvedValue({ deleted: ['运动/a.mp4'], count: 1 })
    vi.mocked(updateMaterial).mockReset().mockResolvedValue({ ...item('a'), elements: [] })
    vi.mocked(uploadMaterial).mockReset().mockResolvedValue({
      task_id: 'task-1', path: '2026-10/新片.mp4', name: '新片.mp4',
    })
  })

  it('加载列表：默认 24 条 / 第一页 / 带回主题计数', async () => {
    const store = useMaterialsStore()
    await store.load()

    expect(listMaterials).toHaveBeenCalledWith({ limit: PAGE_SIZE, offset: 0 })
    expect(store.items.map((entry) => entry.id)).toEqual(['a', 'b'])
    expect(store.total).toBe(2)
    expect(store.dirs).toEqual([{ path: '运动', count: 2 }])
    expect(store.page).toBe(1)
  })

  it('筛选项只带非空值，换条件回到第一页并清空选择', async () => {
    const store = useMaterialsStore()
    await store.load()
    store.toggleSelected('a')
    await store.goPage(2)
    expect(listMaterials).toHaveBeenLastCalledWith({ limit: PAGE_SIZE, offset: PAGE_SIZE })

    await store.applyFilters({ q: '羽毛球', type: 'video', dir: '' })

    expect(listMaterials).toHaveBeenLastCalledWith({
      limit: PAGE_SIZE, offset: 0, q: '羽毛球', type: 'video', dir: '',
    })
    expect(store.selection).toEqual([])
  })

  it('多选：单条切换 / 全选本页 / 清空', async () => {
    const store = useMaterialsStore()
    await store.load()
    store.toggleSelected('a')
    store.toggleSelected('a')
    expect(store.selection).toEqual([])
    store.selectAllOnPage()
    expect(store.selection).toEqual(['a', 'b'])
    expect(store.allSelected).toBe(true)
    store.clearSelection()
    expect(store.selection).toEqual([])
  })

  it('批量删除并发调单条接口，部分失败也更新列表并给出原因', async () => {
    const store = useMaterialsStore()
    await store.load()
    store.selectAllOnPage()
    vi.mocked(trashMaterial)
      .mockResolvedValueOnce({ material_id: 'a', path: 'a', trashed_path: 'a', file_missing: false })
      .mockRejectedValueOnce(new ApiRequestError('素材不存在', 'not_found', 404))

    const failed = await store.trashSelected()

    expect(trashMaterial).toHaveBeenCalledTimes(2)
    expect(failed).toBe(1)
    expect(store.actionError).toContain('1/2 条删除失败')
    expect(store.selection).toEqual([])
    expect(listMaterials).toHaveBeenCalledTimes(2)      // 初次加载 + 删除后刷新
  })

  it('扫描：拿到 task_id 后轮询到 SUCCESS，刷新列表并给出摘要', async () => {
    const store = useMaterialsStore()

    await store.scan()

    expect(scanMaterials).toHaveBeenCalledTimes(1)
    expect(getMaterialTask).toHaveBeenCalledWith('task-1')
    expect(store.taskNote).toBe('好了')
    expect(store.scanning).toBe(false)
    expect(listMaterials).toHaveBeenCalled()
  })

  it('扫描失败时把原因写进 actionError', async () => {
    vi.mocked(getMaterialTask).mockResolvedValue(task('FAILURE', { summary: '素材目录不存在' }))
    const store = useMaterialsStore()

    await store.scan()

    expect(store.actionError).toBe('素材目录不存在')
    expect(store.taskNote).toBeNull()
  })

  it('上传：进度回调写进队列项，索引完成后标记为已入库', async () => {
    vi.mocked(uploadMaterial).mockImplementation(async (_file, onProgress) => {
      onProgress?.(42)
      return { task_id: 'task-1', path: '2026-10/新片.mp4', name: '新片.mp4' }
    })
    const store = useMaterialsStore()
    const file = new File([new Uint8Array([1])], '新片.mp4', { type: 'video/mp4' })

    await store.uploadFiles([file])

    expect(store.uploads[0].name).toBe('新片.mp4')
    expect(store.uploads[0].status).toBe('done')
    expect(store.uploads[0].percent).toBe(100)
    expect(listMaterials).toHaveBeenCalled()
  })

  it('上传失败（如 413）在队列项里留原因', async () => {
    vi.mocked(uploadMaterial).mockRejectedValue(
      new ApiRequestError('文件超过 2 GB 上限', 'payload_too_large', 413))
    const store = useMaterialsStore()

    await store.uploadFiles([new File([new Uint8Array([1])], '大.mp4')])

    expect(store.uploads[0].status).toBe('failed')
    expect(store.uploads[0].message).toContain('2 GB')
  })

  it('任务状态是 FAILURE 但文件真的进了库：按"已入库"收尾（以库为准）', async () => {
    // 并发上传时常见的现象：这一条的任务没抢到（或结果查不到），但另一条任务已经把它入库了
    vi.mocked(getMaterialTask).mockResolvedValue(task('FAILURE', { summary: '没抢到' }))
    vi.mocked(uploadMaterial).mockResolvedValue({
      task_id: 'task-1', path: '2026-10/新片.mp4', name: '新片.mp4',
    })
    vi.mocked(listMaterials).mockResolvedValue({
      ...listing(['a']),
      items: [{ ...item('a'), path: '/app/data/materials/2026-10/新片.mp4' }],
    })
    const store = useMaterialsStore()

    await store.uploadFiles([new File([new Uint8Array([1])], '新片.mp4')])

    expect(store.uploads[0].status).toBe('done')
    expect(store.uploads[0].message).toContain('已入库')
  })

  it('编辑保存后刷新列表并关掉弹窗', async () => {
    const store = useMaterialsStore()
    store.openEdit(item('a'))
    expect(store.editing?.id).toBe('a')
    expect(store.editTags).toEqual(['羽毛球'])

    const ok = await store.saveEdit({ title: '新标题' })

    expect(ok).toBe(true)
    expect(updateMaterial).toHaveBeenCalledWith('a', { title: '新标题' })
    expect(store.editing).toBeNull()
    expect(listMaterials).toHaveBeenCalled()
  })

  it('回收站：打开会拉列表，恢复走任务并刷新两边，清空调 all', async () => {
    vi.mocked(listTrash).mockResolvedValue({
      items: [{ path: '运动/a.mp4', name: 'a.mp4', type: 'video', size_bytes: 10,
                mtime: '2026-10-06T04:00:00+00:00' }],
      total: 1,
    })
    const store = useMaterialsStore()

    await store.openTrash()
    expect(store.trashOpen).toBe(true)
    expect(store.trash.map((entry) => entry.path)).toEqual(['运动/a.mp4'])

    await store.restoreOne('运动/a.mp4')
    expect(restoreTrash).toHaveBeenCalledWith('运动/a.mp4')
    expect(listTrash).toHaveBeenCalledTimes(2)

    await store.purgeAll()
    expect(purgeTrash).toHaveBeenCalledWith({ all: true, confirm: true })
  })

  it('视图模式写进 localStorage（下次打开还是它）', async () => {
    const store = useMaterialsStore()
    store.setView('list')
    expect(window.localStorage.getItem('xhs-materials-view')).toBe('list')
    expect(store.viewMode).toBe('list')
  })
})
