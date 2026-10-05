import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiRequestError, apiFetch, apiUrl } from '@/api/client'

/** 极简 Response 替身：jsdom 环境不保证有 fetch/Response，手搓更稳。 */
function response(body: unknown, status = 200, throwsOnJson = false): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (throwsOnJson) throw new SyntaxError('not json')
      return body
    },
  } as unknown as Response
}

describe('apiUrl', () => {
  it('默认前缀是 /api（配置契约 §五）', () => {
    expect(apiUrl('/runs')).toBe('/api/runs')
  })
})

describe('apiFetch', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('2xx 时返回解析后的 JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ total: 8 })))
    await expect(apiFetch<{ total: number }>('/runs')).resolves.toEqual({ total: 8 })
  })

  it('POST 带 body 时设置 JSON Content-Type 并序列化', async () => {
    const fetchMock = vi.fn(async () => response({}))
    vi.stubGlobal('fetch', fetchMock)
    await apiFetch('/analyze', { method: 'POST', body: { hotspots: ['热点'] } })
    expect(fetchMock).toHaveBeenCalledWith('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hotspots: ['热点'] }),
    })
  })

  it('非 2xx 且是 ErrorResponse 时抛出带 code/message/detail/httpStatus 的错误', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(
      { code: 'conflict', message: '运行尚未完成', detail: { run_id: 'abc' } }, 409,
    )))
    const error = await apiFetch('/runs/abc').catch((err: unknown) => err)
    expect(error).toBeInstanceOf(ApiRequestError)
    expect(error).toMatchObject({
      code: 'conflict',
      message: '运行尚未完成',
      httpStatus: 409,
      detail: { run_id: 'abc' },
    })
  })

  it('错误体不是 JSON 时退回 HTTP 状态文案', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(null, 502, true)))
    const error = await apiFetch('/runs').catch((err: unknown) => err)
    expect(error).toBeInstanceOf(ApiRequestError)
    expect((error as ApiRequestError).message).toBe('请求失败（HTTP 502）')
    expect((error as ApiRequestError).httpStatus).toBe(502)
  })
})
