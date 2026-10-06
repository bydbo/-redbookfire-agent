import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiRequestError, apiFetch, apiUpload, apiUrl, uploadWithProgress } from '@/api/client'

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

  it('PATCH 也走同一套 JSON 编解码（S6.2 编辑素材用它）', async () => {
    const fetchMock = vi.fn(async () => response({ id: 'x' }))
    vi.stubGlobal('fetch', fetchMock)
    await apiFetch('/materials/abc', { method: 'PATCH', body: { title: '新标题' } })
    expect(fetchMock).toHaveBeenCalledWith('/api/materials/abc', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '新标题' }),
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

describe('apiUpload（S6.8 图片热点解析）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function pngFile(): File {
    return new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'a.png', { type: 'image/png' })
  }

  it('POST 原样发 FormData，且不设置 Content-Type（boundary 交给浏览器）', async () => {
    const fetchMock = vi.fn(async () => response({ raw_text: '球场上的球拍特写' }))
    vi.stubGlobal('fetch', fetchMock)
    const form = new FormData()
    form.append('file', pngFile())

    await expect(apiUpload<{ raw_text: string }>('/hotspots/image-clue', form))
      .resolves.toEqual({ raw_text: '球场上的球拍特写' })

    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][]
    expect(calls).toHaveLength(1)
    expect(calls[0][0]).toBe('/api/hotspots/image-clue')
    expect(calls[0][1].method).toBe('POST')
    expect(calls[0][1].headers).toBeUndefined()   // 手写 Content-Type 会把 boundary 写坏
    expect(calls[0][1].body).toBe(form)
  })

  it('非 2xx 时走与 apiFetch 相同的错误归一化', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(
      { code: 'bad_request', message: '只支持 jpg / png / webp 图片（按文件内容判断）', detail: { limit_bytes: 10485760 } },
      400,
    )))

    const error = await apiUpload('/hotspots/image-clue', new FormData()).catch((err: unknown) => err)
    expect(error).toBeInstanceOf(ApiRequestError)
    expect(error).toMatchObject({
      code: 'bad_request',
      message: '只支持 jpg / png / webp 图片（按文件内容判断）',
      httpStatus: 400,
      detail: { limit_bytes: 10485760 },
    })
  })
})

/** 假 XHR：只为验证 `uploadWithProgress` 的进度回调与错误归一化（jsdom 不会真发请求）。 */
class FakeXhr {
  static instances: FakeXhr[] = []

  method = ''
  url = ''
  status = 0
  responseText = ''
  body: FormData | null = null
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null

  constructor() {
    FakeXhr.instances.push(this)
  }

  open(method: string, url: string): void {
    this.method = method
    this.url = url
  }

  send(body: FormData): void {
    this.body = body
  }

  progress(loaded: number, total: number): void {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total } as ProgressEvent)
  }

  finish(status: number, payload: unknown): void {
    this.status = status
    this.responseText = JSON.stringify(payload)
    this.onload?.()
  }
}

describe('uploadWithProgress（S6.5 素材上传）', () => {
  afterEach(() => {
    FakeXhr.instances = []
    vi.unstubAllGlobals()
  })

  it('用 POST 发 FormData 并逐次回调进度', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const percents: number[] = []
    const file = new File([new Uint8Array([1, 2, 3])], '挥拍.mp4', { type: 'video/mp4' })

    const promise = uploadWithProgress<{ task_id: string }>('/materials/uploads', file,
                                                           (percent) => percents.push(percent))
    const xhr = FakeXhr.instances[0]
    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/materials/uploads')
    expect((xhr.body as FormData).get('file')).toBe(file)

    xhr.progress(0, 100)
    xhr.progress(37, 100)
    xhr.progress(100, 100)
    xhr.finish(202, { task_id: 'task-1' })

    await expect(promise).resolves.toEqual({ task_id: 'task-1' })
    expect(percents).toEqual([0, 37, 100])
  })

  it('413 也翻成带 code 的 ApiRequestError（照后端 ErrorResponse）', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const promise = uploadWithProgress('/materials/uploads',
                                       new File([new Uint8Array([1])], '大.mp4'))
    FakeXhr.instances[0].finish(413, {
      code: 'payload_too_large', message: '文件超过 2 GB 上限', detail: { max_size_gb: 2 },
    })

    const error = await promise.catch((err: unknown) => err)
    expect(error).toBeInstanceOf(ApiRequestError)
    expect(error).toMatchObject({ code: 'payload_too_large', httpStatus: 413 })
  })

  it('网络错误（onerror）给出可读文案', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const promise = uploadWithProgress('/materials/uploads',
                                       new File([new Uint8Array([1])], 'a.mp4'))
    FakeXhr.instances[0].onerror?.()
    await expect(promise).rejects.toThrow('网络错误：上传失败')
  })
})
