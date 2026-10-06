import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  chatAttachmentUrl,
  createChatSession,
  getChatSession,
  listChatSessions,
  streamMessage,
} from '@/api/chat'
import { ApiRequestError } from '@/api/client'

/** 极简 Response 替身（与 client.spec.ts 同一套写法）。 */
function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

/** SSE 响应替身：body.getReader() 逐块吐字符串，用来验证跨块的切帧逻辑。 */
function streamResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder()
  let index = 0
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => ({}),
    body: {
      getReader: () => ({
        read: async () => (index < chunks.length
          ? { done: false, value: encoder.encode(chunks[index++]) }
          : { done: true, value: undefined }),
        releaseLock: () => undefined,
      }),
    },
  } as unknown as Response
}

function eventOf(name: string, data: unknown): string {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`
}

/** 取第 n 次 fetch 的 (url, init)；`vi.fn` 的参数元组推断不出来，这里显式断言一次。 */
function callArgs(mock: ReturnType<typeof vi.fn>, index = 0): { url: string; init: RequestInit } {
  const call = mock.mock.calls[index] as unknown as [string, RequestInit]
  return { url: call[0], init: call[1] }
}

describe('对话接口的请求形状', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('建会话是 POST /api/chat/sessions', async () => {
    const fetchMock = vi.fn(async () => response({ session_id: 's-1' }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(createChatSession()).resolves.toEqual({ session_id: 's-1' })
    expect(callArgs(fetchMock).url).toBe('/api/chat/sessions')
    expect(callArgs(fetchMock).init.method).toBe('POST')
  })

  it('会话列表带 limit / offset', async () => {
    const fetchMock = vi.fn(async () => response({ items: [], total: 0 }))
    vi.stubGlobal('fetch', fetchMock)
    await listChatSessions({ limit: 20, offset: 40 })
    expect(callArgs(fetchMock).url).toBe('/api/chat/sessions?limit=20&offset=40')
  })

  it('会话详情默认不带 limit，给了才带', async () => {
    const fetchMock = vi.fn(async () => response({ session_id: 's-1', messages: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await getChatSession('s-1')
    await getChatSession('s-1', 50)
    expect(callArgs(fetchMock, 0).url).toBe('/api/chat/sessions/s-1')
    expect(callArgs(fetchMock, 1).url).toBe('/api/chat/sessions/s-1?limit=50')
  })

  it('附件地址带上会话、消息与下标', () => {
    expect(chatAttachmentUrl('s-1', 'm-1', 0))
      .toBe('/api/chat/sessions/s-1/attachments/m-1/0')
  })
})

describe('streamMessage（SSE 逐帧解析）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('跨块的帧能拼回来，心跳与坏 JSON 被跳过', async () => {
    const fetchMock = vi.fn(async () => streamResponse([
      'event: turn_started\ndata: {"session_id":"s-',
      '1"}\n\nevent: tool_started\ndata: {"tool":"run_hotspot_an',
      'alysis"}\n\n: ping\n\n',
      'event: text_delta\ndata: {"text":"你好"}\n\n',
    ]))
    vi.stubGlobal('fetch', fetchMock)
    const seen: Array<{ event: string; data: Record<string, unknown> }> = []

    await streamMessage('s-1', { text: '帮我蹭一下' }, (event) => seen.push(event))

    expect(seen.map((item) => item.event))
      .toEqual(['turn_started', 'tool_started', 'text_delta'])
    expect(seen[0].data).toEqual({ session_id: 's-1' })
    expect(seen[2].data).toEqual({ text: '你好' })

    const { init } = callArgs(fetchMock)
    expect(init.method).toBe('POST')
    // 故意不设 Content-Type：交给浏览器补 multipart 的 boundary
    expect(init.headers).toBeUndefined()
    const form = init.body as FormData
    expect(form.get('text')).toBe('帮我蹭一下')
    expect(form.get('file')).toBeNull()
  })

  it('末尾没有空行的最后一帧也要吐出来，图片进 multipart', async () => {
    const fetchMock = vi.fn(async () => streamResponse([
      eventOf('turn_finished', { status: 'succeeded' }),
      'event: error\ndata: {"message":"boom"}',
    ]))
    vi.stubGlobal('fetch', fetchMock)
    const seen: string[] = []
    const file = new File([new Uint8Array([1, 2, 3])], '热点.png', { type: 'image/png' })

    await streamMessage('s-1', { file }, (event) => seen.push(event.event))

    expect(seen).toEqual(['turn_finished', 'error'])
    const form = callArgs(fetchMock).init.body as FormData
    expect((form.get('file') as File).name).toBe('热点.png')
    expect(form.get('text')).toBeNull()
  })

  it('非 2xx 抛出与普通接口同一种错误', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(
      { code: 'bad_request', message: '这条消息既没有文字也没有图片' }, 400,
    )))
    const error = await streamMessage('s-1', { text: '' }, () => undefined)
      .catch((err: unknown) => err)
    expect(error).toBeInstanceOf(ApiRequestError)
    expect(error).toMatchObject({ code: 'bad_request', httpStatus: 400 })
  })
})
