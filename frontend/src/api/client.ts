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
 * 带 JSON 编解码的 fetch 封装。非 2xx 时解析 ErrorResponse（{code, message, detail}
 * 平铺结构，见 docs/contracts/openapi.yaml 的 components.responses），解析失败兜底
 * HTTP 状态文案。
 */
export async function apiFetch<T>(path: string, options: ApiFetchOptions = {}): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: options.method ?? 'GET',
    headers: options.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })

  if (!response.ok) {
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
    throw new ApiRequestError(message, code, response.status, detail)
  }

  return (await response.json()) as T
}
