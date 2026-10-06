import type { components } from '@/api/schema'

import { apiFetch, uploadWithProgress } from '@/api/client'

/** 类型全部派生自 openapi-typescript 生成物（AGENTS.md：禁止手写并行类型定义）。 */
export type MaterialItem = components['schemas']['MaterialItem']
export type MaterialDetail = components['schemas']['MaterialDetail']
export type MaterialList = components['schemas']['MaterialList']
export type MaterialTask = components['schemas']['MaterialTask']
export type MaterialTaskAccepted = components['schemas']['MaterialTaskAccepted']
export type MaterialTrashResult = components['schemas']['MaterialTrashResult']
export type MaterialUploadAccepted = components['schemas']['MaterialUploadAccepted']
export type TrashItem = components['schemas']['TrashItem']
export type TrashList = components['schemas']['TrashList']
export type TrashPurgeResult = components['schemas']['TrashPurgeResult']
export type UpdateMaterialRequest = components['schemas']['UpdateMaterialRequest']

export type MaterialType = MaterialItem['type']
export type MaterialSource = MaterialItem['source']

/** 列表筛选：`dir` 传空串表示「只看根目录」，不传表示全部（契约 § /api/materials）。 */
export interface MaterialQuery {
  q?: string
  type?: MaterialType
  source?: MaterialSource
  dir?: string
  limit: number
  offset: number
}

export function listMaterials(query: MaterialQuery): Promise<MaterialList> {
  const params = new URLSearchParams({
    limit: String(query.limit),
    offset: String(query.offset),
  })
  if (query.q) params.set('q', query.q)
  if (query.type) params.set('type', query.type)
  if (query.source) params.set('source', query.source)
  if (query.dir !== undefined) params.set('dir', query.dir)
  return apiFetch<MaterialList>(`/materials?${params.toString()}`)
}

/** 单条详情（含爆点要素）；非法或未知 id 由后端按 404 返回。 */
export function getMaterial(materialId: string): Promise<MaterialDetail> {
  return apiFetch<MaterialDetail>(`/materials/${encodeURIComponent(materialId)}`)
}

/** 改标题 / 标签 / 描述：后端写库 + 写 `<素材>.txt` 旁车（S6.2）。 */
export function updateMaterial(materialId: string,
                               body: UpdateMaterialRequest): Promise<MaterialDetail> {
  return apiFetch<MaterialDetail>(`/materials/${encodeURIComponent(materialId)}`,
                                  { method: 'PATCH', body })
}

export function trashMaterial(materialId: string): Promise<MaterialTrashResult> {
  return apiFetch<MaterialTrashResult>(
    `/materials/${encodeURIComponent(materialId)}/trash`, { method: 'POST' })
}

export function listTrash(): Promise<TrashList> {
  return apiFetch<TrashList>('/materials/trash')
}

/** 从回收站恢复（= 重新入库，新 uuid）；返回的 task_id 用来轮询索引进度。 */
export function restoreTrash(path: string): Promise<MaterialTaskAccepted> {
  return apiFetch<MaterialTaskAccepted>('/materials/trash/restore',
                                        { method: 'POST', body: { path } })
}

/** 真删（不可恢复）：`paths` 单条或 `all` 清空，`confirm` 必须为 true（二次确认）。 */
export function purgeTrash(body: { paths?: string[]; all?: boolean; confirm: boolean })
  : Promise<TrashPurgeResult> {
  return apiFetch<TrashPurgeResult>('/materials/trash/purge', { method: 'POST', body })
}

/** 触发一次「增量同步 + 向量回填」，返回 task_id 供轮询。 */
export function scanMaterials(): Promise<MaterialTaskAccepted> {
  return apiFetch<MaterialTaskAccepted>('/materials/scan', { method: 'POST' })
}

export function getMaterialTask(taskId: string): Promise<MaterialTask> {
  return apiFetch<MaterialTask>(`/materials/tasks/${encodeURIComponent(taskId)}`)
}

/** 上传单个素材（XHR + 进度）；多文件由调用方逐个并发调用，各自独立进度与失败。 */
export function uploadMaterial(file: File,
                               onProgress?: (percent: number) => void)
  : Promise<MaterialUploadAccepted> {
  return uploadWithProgress<MaterialUploadAccepted>('/materials/uploads', file, onProgress)
}
