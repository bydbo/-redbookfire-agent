import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiRequestError } from '@/api/client'
import type {
  MaterialItem,
  MaterialSource,
  MaterialTask,
  MaterialType,
  TrashItem,
  UpdateMaterialRequest,
} from '@/api/materials'
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

/** 素材库页的视图模式（存 localStorage，页面之间保持一致）。 */
export type ViewMode = 'grid' | 'list'

/** 一次上传的生命周期：上传中 → 索引中 → 完成 / 失败。 */
export type UploadStatus = 'uploading' | 'indexing' | 'done' | 'failed'

export interface UploadEntry {
  name: string
  percent: number
  status: UploadStatus
  message: string
}

export interface MaterialFilters {
  q: string
  type: MaterialType | null
  source: MaterialSource | null
  /** `null` = 全部；空串 = 只看根目录；其它 = 该主题目录 */
  dir: string | null
}

export const PAGE_SIZE = 24
const POLL_INTERVAL_MS = 1500
/** 轮询上限（≈6 分钟）：超了就当作"还在跑"，不让页面永远挂着。 */
const MAX_POLLS = 240
const TERMINAL_STATES = ['SUCCESS', 'FAILURE']
const VIEW_KEY = 'xhs-materials-view'

function initialViewMode(): ViewMode {
  try {
    return window.localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'grid'
  } catch {
    return 'grid'
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

function describeError(err: unknown): string {
  if (err instanceof ApiRequestError) return err.message
  if (err instanceof Error) return err.message
  return '未知错误'
}

export const useMaterialsStore = defineStore('materials', () => {
  const items = ref<MaterialItem[]>([])
  const dirs = ref<{ path: string; count: number }[]>([])
  const total = ref(0)
  const offset = ref(0)
  const filters = ref<MaterialFilters>({ q: '', type: null, source: null, dir: null })
  const viewMode = ref<ViewMode>(initialViewMode())
  const loading = ref(false)
  const failure = ref<string | null>(null)
  const selection = ref<string[]>([])

  const uploads = ref<UploadEntry[]>([])
  const scanning = ref(false)
  /** 扫描 / 恢复这类后台任务的人话进度（显示在工具条下面） */
  const taskNote = ref<string | null>(null)
  const actionError = ref<string | null>(null)

  const trash = ref<TrashItem[]>([])
  const trashOpen = ref(false)
  const trashLoading = ref(false)

  const editing = ref<MaterialItem | null>(null)
  const editTags = ref<string[]>([])
  const editSaving = ref(false)

  const page = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
  const selectedOnPage = computed(
    () => items.value.filter((item) => selection.value.includes(item.id)).length)
  const allSelected = computed(
    () => items.value.length > 0 && selectedOnPage.value === items.value.length)
  const busy = computed(() => scanning.value || uploads.value.some(
    (entry) => entry.status === 'uploading' || entry.status === 'indexing'))

  // ---------- 列表 ----------

  function query(): Parameters<typeof listMaterials>[0] {
    const current = filters.value
    return {
      limit: PAGE_SIZE,
      offset: offset.value,
      ...(current.q.trim() ? { q: current.q.trim() } : {}),
      ...(current.type ? { type: current.type } : {}),
      ...(current.source ? { source: current.source } : {}),
      ...(current.dir === null ? {} : { dir: current.dir }),
    }
  }

  async function load(): Promise<void> {
    loading.value = true
    failure.value = null
    try {
      const payload = await listMaterials(query())
      items.value = payload.items
      dirs.value = payload.dirs
      total.value = payload.total
    } catch (err) {
      failure.value = describeError(err)
    } finally {
      loading.value = false
    }
  }

  async function applyFilters(patch: Partial<MaterialFilters>): Promise<void> {
    filters.value = { ...filters.value, ...patch }
    offset.value = 0                 // 换筛选条件就回第一页
    selection.value = []
    await load()
  }

  async function clearFilters(): Promise<void> {
    await applyFilters({ q: '', type: null, source: null, dir: null })
  }

  async function goPage(target: number): Promise<void> {
    offset.value = Math.max(0, (Math.max(1, target) - 1) * PAGE_SIZE)
    selection.value = []
    await load()
  }

  function setView(mode: ViewMode): void {
    viewMode.value = mode
    try {
      window.localStorage.setItem(VIEW_KEY, mode)
    } catch {
      // 隐私模式下存不进去也不影响本次会话
    }
  }

  // ---------- 多选 ----------

  function toggleSelected(id: string): void {
    selection.value = selection.value.includes(id)
      ? selection.value.filter((item) => item !== id)
      : [...selection.value, id]
  }

  function selectAllOnPage(): void {
    selection.value = items.value.map((item) => item.id)
  }

  function clearSelection(): void {
    selection.value = []
  }

  // ---------- 任务轮询 ----------

  async function pollTask(taskId: string, onUpdate?: (task: MaterialTask) => void)
    : Promise<MaterialTask | null> {
    if (!taskId) return null
    let last: MaterialTask | null = null
    for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
      last = await getMaterialTask(taskId)
      onUpdate?.(last)
      if (TERMINAL_STATES.includes(last.state)) return last
      await delay(POLL_INTERVAL_MS)
    }
    return last
  }

  /** 跑一次索引任务并刷新列表；返回是否成功（失败时把原因放进 `actionError`）。 */
  async function runIndexTask(taskId: string, label: string): Promise<boolean> {
    taskNote.value = `${label}中…`
    try {
      const task = await pollTask(taskId, (current) => {
        if (current.step) taskNote.value = `${label}：${current.step}`
      })
      if (task?.state === 'SUCCESS') {
        taskNote.value = task.summary ?? `${label}完成`
        return true
      }
      taskNote.value = null
      actionError.value = task?.summary ?? `${label}失败`
      return false
    } catch (err) {
      taskNote.value = null
      actionError.value = describeError(err)
      return false
    }
  }

  // ---------- 扫描 / 上传 ----------

  async function scan(): Promise<void> {
    actionError.value = null
    scanning.value = true
    try {
      const accepted = await scanMaterials()
      await runIndexTask(accepted.task_id, '扫描素材库')
      await load()
    } catch (err) {
      actionError.value = describeError(err)
    } finally {
      scanning.value = false
    }
  }

  async function uploadFiles(files: File[]): Promise<void> {
    actionError.value = null
    for (const file of files) {
      uploads.value = [
        { name: file.name, percent: 0, status: 'uploading', message: '' },
        ...uploads.value,
      ]
    }
    // 逐个并发上传：每个文件独立进度与失败，互不拖累（契约就是单文件接口）
    await Promise.all(files.map((file) => uploadOne(file)))
    await load()
  }

  /** 上传的文件是否真的进了库（任务状态不可靠时的兜底判据：磁盘/数据库才是权威源）。 */
  async function listedInLibrary(path: string): Promise<boolean> {
    try {
      const payload = await listMaterials({ limit: PAGE_SIZE, offset: 0 })
      return payload.items.some((item) => item.path.endsWith(path))
    } catch {
      return false
    }
  }

  async function uploadOne(file: File): Promise<void> {
    const entry = () => uploads.value.find((item) => item.name === file.name)
    try {
      const accepted = await uploadMaterial(file, (percent) => {
        const current = entry()
        if (current) current.percent = percent
      })
      const pending = entry()
      if (pending) {
        pending.status = 'indexing'
        pending.percent = 100
      }
      const task = await pollTask(accepted.task_id, (current) => {
        const item = entry()
        if (item && current.step) item.message = current.step
      })
      // 任务状态可能是 FAILURE（并发任务里"这一条没抢到"）甚至查不到——**以库为准**：
      // 只要这个文件真的进了素材库，对用户来说就是成功（磁盘与数据库才是权威源）。
      const indexed = task?.state === 'SUCCESS' || await listedInLibrary(accepted.path)
      const done = entry()
      if (done) {
        done.status = indexed ? 'done' : 'failed'
        done.message = task?.state === 'SUCCESS'
          ? (task.summary ?? '已入库')
          : indexed ? '已入库（另一条索引任务先跑完了）' : (task?.summary ?? '索引失败')
      }
    } catch (err) {
      const failed = entry()
      if (failed) {
        failed.status = 'failed'
        failed.message = describeError(err)
      }
    }
  }

  function clearUploads(): void {
    uploads.value = []
  }

  // ---------- 编辑 ----------

  function openEdit(item: MaterialItem): void {
    editing.value = item
    editTags.value = [...item.tags]
  }

  function closeEdit(): void {
    editing.value = null
    editTags.value = []
  }

  async function saveEdit(patch: UpdateMaterialRequest): Promise<boolean> {
    if (!editing.value) return false
    editSaving.value = true
    actionError.value = null
    try {
      await updateMaterial(editing.value.id, patch)
      closeEdit()
      await load()
      return true
    } catch (err) {
      actionError.value = describeError(err)
      return false
    } finally {
      editSaving.value = false
    }
  }

  // ---------- 回收站 ----------

  async function refreshTrash(): Promise<void> {
    trashLoading.value = true
    try {
      const payload = await listTrash()
      trash.value = payload.items
    } catch (err) {
      actionError.value = describeError(err)
    } finally {
      trashLoading.value = false
    }
  }

  async function openTrash(): Promise<void> {
    trashOpen.value = true
    await refreshTrash()
  }

  function closeTrash(): void {
    trashOpen.value = false
  }

  function removeFromSelection(id: string): void {
    selection.value = selection.value.filter((item) => item !== id)
  }

  /** 移入回收站：单条；失败返回 false 并把原因写进 `actionError`。 */
  async function trashOne(id: string): Promise<boolean> {
    actionError.value = null
    try {
      await trashMaterial(id)
      removeFromSelection(id)
      await load()
      if (trashOpen.value) await refreshTrash()
      return true
    } catch (err) {
      actionError.value = describeError(err)
      return false
    }
  }

  /** 批量删除：并发调单条接口，汇总失败条数（部分失败不影响其它条目）。 */
  async function trashSelected(): Promise<number> {
    const targets = [...selection.value]
    if (targets.length === 0) return 0
    actionError.value = null
    const results = await Promise.allSettled(targets.map((id) => trashMaterial(id)))
    const failed = results.filter((result) => result.status === 'rejected').length
    if (failed > 0) {
      const first = results.find((result) => result.status === 'rejected')
      actionError.value = `${failed}/${targets.length} 条删除失败：`
        + describeError(first && first.status === 'rejected' ? first.reason : null)
    }
    selection.value = []
    await load()
    if (trashOpen.value) await refreshTrash()
    return failed
  }

  async function restoreOne(path: string): Promise<boolean> {
    actionError.value = null
    try {
      const accepted = await restoreTrash(path)
      const ok = await runIndexTask(accepted.task_id, '恢复后重新入库')
      await refreshTrash()
      await load()
      return ok
    } catch (err) {
      actionError.value = describeError(err)
      return false
    }
  }

  async function purgePaths(paths: string[]): Promise<boolean> {
    actionError.value = null
    try {
      await purgeTrash({ paths, confirm: true })
      await refreshTrash()
      return true
    } catch (err) {
      actionError.value = describeError(err)
      return false
    }
  }

  async function purgeAll(): Promise<boolean> {
    actionError.value = null
    try {
      await purgeTrash({ all: true, confirm: true })
      await refreshTrash()
      return true
    } catch (err) {
      actionError.value = describeError(err)
      return false
    }
  }

  return {
    items, dirs, total, offset, filters, viewMode, loading, failure, selection,
    uploads, scanning, taskNote, actionError, trash, trashOpen, trashLoading,
    editing, editTags, editSaving, page, pageCount, selectedOnPage, allSelected, busy,
    load, applyFilters, clearFilters, goPage, setView,
    toggleSelected, selectAllOnPage, clearSelection,
    scan, uploadFiles, clearUploads,
    openEdit, closeEdit, saveEdit,
    openTrash, closeTrash, refreshTrash, trashOne, trashSelected, restoreOne,
    purgePaths, purgeAll,
  }
})
