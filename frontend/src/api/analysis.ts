import type { components } from '@/api/schema'

import { apiFetch, apiUpload, apiUrl } from '@/api/client'

/** 类型全部派生自 openapi-typescript 生成物（AGENTS.md：禁止手写并行类型定义）。 */
export type AnalyzeRequest = components['schemas']['AnalyzeRequest']
export type AnalyzeAccepted = components['schemas']['AnalyzeAccepted']
export type JobStatus = components['schemas']['JobStatus']
export type RunDetail = components['schemas']['RunDetail']
export type RunSummary = components['schemas']['RunSummary']
export type RunList = components['schemas']['RunList']
export type HotspotResult = components['schemas']['HotspotResult']
export type MatchCandidate = components['schemas']['MatchCandidate']
export type Element = components['schemas']['Element']
export type HotspotClue = components['schemas']['HotspotClue']
export type ImageClueResult = components['schemas']['ImageClueResult']

/** 爆点要素类型的中文标签（口径见 docs/产品方案.md 第六节）；结果页与图片解析卡片共用。 */
export const ELEMENT_TYPE_LABELS: Record<Element['type'], string> = {
  ip: 'IP',
  topic: '主题',
  scene: '场景',
  visual: '画面',
  emotion: '情绪',
  sound: '声音',
  conflict: '冲突',
  format: '形式',
  audience: '人群',
}

/** 九类要素的可选值（顺序即产品方案 §六的枚举顺序），供下拉框渲染。 */
export const ELEMENT_TYPES = Object.keys(ELEMENT_TYPE_LABELS) as Element['type'][]

/** 提交热点分析任务；后端落库后立即返回 202，进度经 getJob 轮询。 */
export function submitAnalysis(body: AnalyzeRequest): Promise<AnalyzeAccepted> {
  return apiFetch<AnalyzeAccepted>('/analyze', { method: 'POST', body })
}

/** 轮询任务状态；404 表示 job 不存在（ApiRequestError.code = not_found）。 */
export function getJob(jobId: string): Promise<JobStatus> {
  return apiFetch<JobStatus>(`/jobs/${encodeURIComponent(jobId)}`)
}

/** 读取运行结果；运行未完成时后端返回 409（ApiRequestError.httpStatus = 409）。 */
export function getRun(runId: string): Promise<RunDetail> {
  return apiFetch<RunDetail>(`/runs/${encodeURIComponent(runId)}`)
}

/** 历史运行列表（S5.6）：后端按 created_at 倒序分页，limit 1–100、offset ≥ 0。 */
export function listRuns(params: { limit: number; offset: number }): Promise<RunList> {
  const query = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  })
  return apiFetch<RunList>(`/runs?${query.toString()}`)
}

/** 素材关键帧图片地址（S5.4 取帧接口），可直接用于 <img src>。 */
export function getKeyframeUrl(materialId: string, index: number): string {
  return apiUrl(`/materials/${encodeURIComponent(materialId)}/keyframes/${index}`)
}

/**
 * 图片热点解析（S6.6 接口，S6.8 前端入口）：上传一张图，拿回热点描述 + 爆点线索。
 * multipart 单文件字段 `file`；后端只在内存里转成模型输入，不落盘、不入库，结果也不进 `runs` 统计。
 */
export function parseImageClue(file: File): Promise<ImageClueResult> {
  const form = new FormData()
  form.append('file', file)
  return apiUpload<ImageClueResult>('/hotspots/image-clue', form)
}
