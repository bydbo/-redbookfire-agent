import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ArtifactVersion, ChatContext, ChatSessionDetail, ContextActionResponse } from '@/api/chat'
import * as api from '@/api/chat'
import { useChatStore } from '@/stores/chat'

vi.mock('@/api/chat', () => ({
  createChatSession: vi.fn(), deleteChatSession: vi.fn(), getChatSession: vi.fn(),
  getChatContext: vi.fn(), listChatSessions: vi.fn(), streamMessage: vi.fn(),
  postChatContextAction: vi.fn(), listChatArtifacts: vi.fn(), getChatArtifact: vi.fn(),
  createClueVersion: vi.fn(), selectClueVersion: vi.fn(),
}))
vi.mock('@/api/analysis', () => ({ getRun: vi.fn() }))

const ctx = (id = 's1', stateVersion = 1): ChatContext => ({
  session_id: id, state_version: stateVersion, topic_id: 't1', recovery_status: 'complete',
  facts: [], proposals: [], artifact_heads: [], effective_mode: 'enforce', blocking_proposals: 0,
  object_refs: [], recovery: { status: 'complete' },
})
const detail = (id: string): ChatSessionDetail => ({
  session_id: id, title: id, created_at: '', updated_at: '', messages: [],
})
const artifact = (id = 'v1'): ArtifactVersion => ({
  session_id: 's1', version_id: id, object_key: 'clue:images:group1', object_type: 'clue',
  status: 'succeeded', version_no: 1, prompt_versions: {}, usage: {}, created_at: '',
  content: { raw_text: 'C0', clue: {} }, source: {}, evidence_snapshot: {},
})
const action = (context: ChatContext): ContextActionResponse => ({
  session_id: context.session_id, state_version: context.state_version,
  action: 'recover', decision: 'applied', context, status: 'succeeded',
})
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('上下文、版本与切会话隔离', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(api.getChatSession).mockImplementation(async id => detail(id))
    vi.mocked(api.getChatContext).mockImplementation(async id => ctx(id))
    vi.mocked(api.listChatSessions).mockResolvedValue({ items: [], total: 0 })
  })
  afterEach(() => vi.restoreAllMocks())

  it('切会话立即清理旧选择、上下文、版本和来源，迟到响应不覆盖新会话', async () => {
    const store = useChatStore()
    store.currentId = 's1'
    store.context = ctx()
    store.selectedBase = { objectKey: 'draft:r1:1', versionId: 'v1', versionNo: 1 }
    store.artifactPreview = artifact()
    const old = deferred<ChatContext>()
    vi.mocked(api.getChatContext).mockReturnValueOnce(old.promise)
    const pending = store.loadContext()
    const next = deferred<ChatSessionDetail>()
    vi.mocked(api.getChatSession).mockReturnValueOnce(next.promise)
    const open = store.openSession('s2')
    expect(store.context).toBeNull()
    expect(store.selectedBase).toBeNull()
    expect(store.artifactPreview).toBeNull()
    old.resolve(ctx('s1', 99))
    await pending
    expect(store.context).toBeNull()
    next.resolve(detail('s2'))
    await open
    await store.loadContext()
    expect(store.context?.session_id).toBe('s2')
    store.reset()
  })

  it('旧会话迟到错误不污染新会话的错误或加载状态', async () => {
    const store = useChatStore()
    store.currentId = 's1'
    const old = deferred<ChatContext>()
    vi.mocked(api.getChatContext).mockReturnValueOnce(old.promise)
    const load = store.loadContext()
    await store.openSession('s2')
    await store.loadContext()
    old.reject(new Error('old failure'))
    await load
    expect(store.contextError).toBeNull()
    expect(store.contextLoading).toBe(false)
    store.reset()
  })

  it('恢复续页与失败重试携带同一恢复身份及页动作，完成默认不确认缺口', async () => {
    const store = useChatStore()
    store.currentId = 's1'
    store.context = { ...ctx(), recovery_status: 'partial', recovery: {
      status: 'failed', recovery_id: 'recovery1', page_action_id: 'old-page',
    } }
    vi.mocked(api.postChatContextAction).mockResolvedValue(action(store.context))
    await store.startRecovery()
    expect(api.postChatContextAction).toHaveBeenLastCalledWith('s1', expect.objectContaining({
      action: 'recover', recovery_id: 'recovery1', retry_of: 'old-page',
      expected_state_version: 1, action_id: expect.any(String),
    }))
    await store.completeRecovery()
    expect(api.postChatContextAction).toHaveBeenLastCalledWith('s1', expect.objectContaining({
      action: 'complete_recovery', recovery_id: 'recovery1', acknowledge_partial_history: false,
    }))
  })

  it('版本历史和预览响应按请求顺序与会话隔离', async () => {
    const store = useChatStore()
    store.currentId = 's1'
    const old = deferred<ArtifactVersion>()
    vi.mocked(api.getChatArtifact).mockReturnValueOnce(old.promise).mockResolvedValueOnce(artifact('v2'))
    const first = store.previewArtifact('v1')
    await store.previewArtifact('v2')
    old.resolve(artifact('v1'))
    await first
    expect(store.artifactPreview?.version_id).toBe('v2')
    const oldList = deferred<{ items: ArtifactVersion[]; total: number }>()
    vi.mocked(api.listChatArtifacts).mockReturnValueOnce(oldList.promise)
    const list = store.loadArtifacts('clue:images:group1')
    store.reset()
    oldList.resolve({ items: [artifact()], total: 1 })
    await list
    expect(store.artifactVersions).toEqual([])
  })

  it('旧版改稿发送明确绑定，并在发送后清除选择', async () => {
    const store = useChatStore()
    store.currentId = 's1'
    store.context = ctx()
    store.selectedBase = { objectKey: 'draft:r1:1', versionId: 'old-v1', versionNo: 1 }
    vi.mocked(api.streamMessage).mockResolvedValue(undefined)
    await store.send({ text: '缩短这版' })
    const input = vi.mocked(api.streamMessage).mock.calls[0]?.[1]
    expect(JSON.parse(input?.operationBinding ?? '{}')).toEqual({
      operation: 'revise_draft', object_key: 'draft:r1:1', base_version_id: 'old-v1', expected_state_version: 1,
    })
    expect(store.selectedBase).toBeNull()
  })
})
