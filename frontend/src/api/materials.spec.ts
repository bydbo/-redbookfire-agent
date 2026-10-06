import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  getMaterial,
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

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

function fetchMock(body: unknown = {}, status = 200) {
  const mock = vi.fn(async () => response(body, status))
  vi.stubGlobal('fetch', mock)
  return mock
}

function lastCall(mock: ReturnType<typeof fetchMock>): [string, RequestInit | undefined] {
  const calls = mock.mock.calls as unknown as [string, RequestInit | undefined][]
  return calls[calls.length - 1]
}

describe('materials 接口层（S6.5）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('listMaterials 只带上真正给了的筛选条件', async () => {
    const mock = fetchMock({ items: [], total: 0, limit: 24, offset: 0, dirs: [] })

    await listMaterials({ limit: 24, offset: 0 })
    expect(lastCall(mock)[0]).toBe('/api/materials?limit=24&offset=0')

    await listMaterials({ q: '羽毛球', type: 'video', source: 'sidecar', dir: '运动',
                          limit: 24, offset: 48 })
    const url = lastCall(mock)[0]
    expect(url).toContain('limit=24')
    expect(url).toContain('offset=48')
    expect(url).toContain('q=%E7%BE%BD%E6%AF%9B%E7%90%83')
    expect(url).toContain('type=video')
    expect(url).toContain('source=sidecar')
    expect(url).toContain('dir=%E8%BF%90%E5%8A%A8')
  })

  it('dir 传空串表示"只看根目录"（不能当成"没传"）', async () => {
    const mock = fetchMock({ items: [], total: 0, limit: 24, offset: 0, dirs: [] })
    await listMaterials({ dir: '', limit: 24, offset: 0 })
    expect(lastCall(mock)[0]).toContain('dir=')
  })

  it('详情 / 编辑 / 删除 / 恢复 / 清空各自打对的路径与方法', async () => {
    const mock = fetchMock({ id: 'm-1' })

    await getMaterial('m-1')
    expect(lastCall(mock)[0]).toBe('/api/materials/m-1')
    expect(lastCall(mock)[1]?.method ?? 'GET').toBe('GET')

    await updateMaterial('m-1', { title: '新标题', tags: ['a'], description: '说明' })
    const [, editInit] = lastCall(mock)
    expect(editInit?.method).toBe('PATCH')
    expect(JSON.parse(String(editInit?.body))).toEqual({
      title: '新标题', tags: ['a'], description: '说明',
    })

    await trashMaterial('m-1')
    expect(lastCall(mock)[0]).toBe('/api/materials/m-1/trash')
    expect(lastCall(mock)[1]?.method).toBe('POST')

    await listTrash()
    expect(lastCall(mock)[0]).toBe('/api/materials/trash')

    await restoreTrash('运动/挥拍.mp4')
    expect(lastCall(mock)[0]).toBe('/api/materials/trash/restore')
    expect(JSON.parse(String(lastCall(mock)[1]?.body))).toEqual({ path: '运动/挥拍.mp4' })

    await purgeTrash({ all: true, confirm: true })
    expect(lastCall(mock)[0]).toBe('/api/materials/trash/purge')
    expect(JSON.parse(String(lastCall(mock)[1]?.body))).toEqual({ all: true, confirm: true })

    await scanMaterials()
    expect(lastCall(mock)[0]).toBe('/api/materials/scan')
    expect(lastCall(mock)[1]?.method).toBe('POST')

    await getMaterialTask('task-1')
    expect(lastCall(mock)[0]).toBe('/api/materials/tasks/task-1')
  })

  it('uploadMaterial 走 XHR 的 multipart 路径（不写 Content-Type）', () => {
    class RecordingXhr {
      static instances: RecordingXhr[] = []

      openArgs: [string, string] | null = null
      body: FormData | null = null
      upload: Record<string, unknown> = {}

      constructor() {
        RecordingXhr.instances.push(this)
      }

      open(method: string, url: string): void {
        this.openArgs = [method, url]
      }

      send(body: FormData): void {
        this.body = body
      }
    }
    vi.stubGlobal('XMLHttpRequest', RecordingXhr)
    const file = new File([new Uint8Array([1])], 'a.mp4', { type: 'video/mp4' })

    void uploadMaterial(file)

    const xhr = RecordingXhr.instances[0]
    expect(xhr.openArgs).toEqual(['POST', '/api/materials/uploads'])
    expect(xhr.body?.get('file')).toBe(file)
  })
})
