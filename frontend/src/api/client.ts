import type { components } from '@/api/schema'

export type ErrorResponse = components['schemas']['ErrorResponse']

/** 请求失败的统一错误：优先携带后端 ErrorResponse 的 code/message/detail。 */
export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly code?: string,
    readonly httpStatus?: number,
    readonly detail?: unknown,
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

// 接口前缀：配置契约 §五约定读取 VITE_API_BASE_URL，默认 /api（开发与生产同源，无需改代码）
const apiBase: string = import.meta.env.VITE_API_BASE_URL ?? '/api'

/** 拼接接口地址；path 以「去掉 /api 前缀」的契约路径传入，如 /analyze、/jobs/{job_id}。 */
export function apiUrl(path: string): string {
  return `${apiBase}${path}`
}

interface ApiFetchOptions {
  method?: 'GET' | 'POST'
  body?: unknown
}

/**
 * 非 2xx 时解析 ErrorResponse（{code, message, detail} 平铺结构，见
 * docs/contracts/openapi.yaml 的 components.responses），解析失败兜底 HTTP 状态文案。
 * `apiFetch` 与 `apiUpload` 共用，保证两条路径抛出同一种错误。
 */
async function normalizeError(response: Response): Promise<ApiRequestError> {
  let code: string | undefined
  let message = `请求失败（HTTP ${response.status}）`
  let detail: unknown
  try {
    const body: unknown = await response.json()
    if (body !== null && typeof body === 'object' && 'code' in body && 'message' in body) {
      const error = body as Partial<ErrorResponse>
      code = error.code
      message = error.message ?? message
      detail = error.detail
    }
  } catch {
    // 保留兜底文案（后端未返回 JSON 的错误页）
  }
  return new ApiRequestError(message, code, response.status, detail)
}

/** 带 JSON 编解码的 fetch 封装。 */
export async function apiFetch<T>(path: string, options: ApiFetchOptions = {}): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: options.method ?? 'GET',
    headers: options.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })

  if (!response.ok) throw await normalizeError(response)

  return (await response.json()) as T
}

/**
 * multipart/form-data 上传（S6.8 图片热点解析用）。**故意不设 Content-Type**：
 * 交给浏览器补 `multipart/form-data; boundary=…`，手写会把 boundary 写坏。
 * 错误归一化与 `apiFetch` 完全一致（同一套 code / message / detail）。
 */
export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(apiUrl(path), { method: 'POST', body: form })

  if (!response.ok) throw await normalizeError(response)

  return (await response.json()) as T
}
