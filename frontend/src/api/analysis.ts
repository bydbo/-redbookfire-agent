import type { components } from '@/api/schema'

import { apiFetch } from '@/api/client'

/** 类型全部派生自 openapi-typescript 生成物（AGENTS.md：禁止手写并行类型定义）。 */
export type AnalyzeRequest = components['schemas']['AnalyzeRequest']
export type AnalyzeAccepted = components['schemas']['AnalyzeAccepted']
export type JobStatus = components['schemas']['JobStatus']
export type RunDetail = components['schemas']['RunDetail']

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
