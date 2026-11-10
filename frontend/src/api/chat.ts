import type { components } from '@/api/schema'

import { ApiRequestError, apiFetch, apiUrl, normalizeResponseError } from '@/api/client'

// 类型全部派生自 openapi-typescript 生成物（AGENTS.md：禁止手写并行类型定义）。
export type ChatSession = components['schemas']['ChatSession']
export type ChatSessionRename = components['schemas']['ChatSessionRename']
export type ChatSessionSummary = components['schemas']['ChatSessionSummary']
export type ChatSessionList = components['schemas']['ChatSessionList']
export type ChatSessionDetail = components['schemas']['ChatSessionDetail']
export type ChatMessage = components['schemas']['ChatMessage']
export type ChatToolCall = components['schemas']['ChatToolCall']
export type ChatAttachment = components['schemas']['ChatAttachment']
// E7 第六批（S7.24 / S7.25）：上下文状态与产物版本
export type ChatContext = components['schemas']['ChatContext']
export type ContextFact = components['schemas']['ContextFact']
export type ContextProposal = components['schemas']['ContextProposal']
export type ArtifactHead = components['schemas']['ArtifactHead']
export type ContextActionRequest = components['schemas']['ContextActionRequest']
export type ContextActionResponse = components['schemas']['ContextActionResponse']
export type ArtifactVersion = components['schemas']['ArtifactVersion']
export type ArtifactVersionList = components['schemas']['ArtifactVersionList']
export type ObjectRef = components['schemas']['ObjectRef']
export type RecoveryProgress = components['schemas']['RecoveryProgress']
export type ClueVersionRequest = components['schemas']['ClueVersionRequest']

// 建一个空会话（前端「新对话」）；标题等首条用户消息落库时自动补。
export function createChatSession(): Promise<ChatSession> {
  return apiFetch<ChatSession>('/chat/sessions', { method: 'POST' })
}

// 会话列表：updated_at 倒序（平局按 id 倒序），带消息数与末条预览。
export function listChatSessions(params: { limit: number; offset: number })
  : Promise<ChatSessionList> {
  const query = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  })
  return apiFetch<ChatSessionList>(`/chat/sessions?${query.toString()}`)
}

// 重命名会话（S7.30）：只改展示名，不改 updated_at / 消息 / 上下文。
export function renameChatSession(sessionId: string, title: string): Promise<ChatSession> {
  const body: ChatSessionRename = { title }
  return apiFetch<ChatSession>(`/chat/sessions/${encodeURIComponent(sessionId)}`,
                               { method: 'PATCH', body })
}

// 会话详情（含消息，按时间升序）；limit 只取最近 N 条。
export function getChatSession(sessionId: string, limit?: number): Promise<ChatSessionDetail> {
  const path = `/chat/sessions/${encodeURIComponent(sessionId)}`
  const suffix = limit === undefined ? '' : `?limit=${String(limit)}`
  return apiFetch<ChatSessionDetail>(`${path}${suffix}`)
}

// 会话内附件地址（回看缩略图用），可直接当 img 的 src。
export function chatAttachmentUrl(sessionId: string, messageId: string, index: number): string {
  return apiUrl(`/chat/sessions/${encodeURIComponent(sessionId)}`
                + `/attachments/${encodeURIComponent(messageId)}/${String(index)}`)
}

// 会话上下文（S7.24）：生效 / 待确认事实、状态版本、恢复状态与产物 head。
export function getChatContext(sessionId: string): Promise<ChatContext> {
  return apiFetch<ChatContext>(`/chat/sessions/${encodeURIComponent(sessionId)}/context`)
}

// 上下文动作（S7.24）：confirm / reject / retract / replace / reset_topic。
// `expected_state_version` 是 CAS 条件：与服务端不一致时返回 409（调用方刷新后重试）。
export function postChatContextAction(
  sessionId: string,
  body: ContextActionRequest,
): Promise<ContextActionResponse> {
  return apiFetch<ContextActionResponse>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/context/actions`,
    { method: 'POST', body },
  )
}

// 按对象列版本（S7.25）；不传 object_key 时给每个对象的最新成功版本。
export function listChatArtifacts(
  sessionId: string,
  objectKey?: string,
): Promise<ArtifactVersionList> {
  const query = objectKey === undefined || objectKey === ''
    ? ''
    : `?object_key=${encodeURIComponent(objectKey)}`
  return apiFetch<ArtifactVersionList>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/artifacts${query}`,
  )
}

// 删除会话（v2 §3.3D）：级联状态 / 版本 / 缓存 / 观测，并清理该会话附件。
export function deleteChatSession(sessionId: string): Promise<void> {
  return apiFetch<void>(`/chat/sessions/${encodeURIComponent(sessionId)}`,
                       { method: 'DELETE' })
}

// 保存用户编辑的线索版本（v2 §3.3C）。
export function createClueVersion(
  sessionId: string,
  body: ClueVersionRequest,
): Promise<ContextActionResponse> {
  return apiFetch<ContextActionResponse>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/artifacts/clue-versions`,
    { method: 'POST', body },
  )
}

// 选定一个线索版本（v2 §3.3C）。
export function selectClueVersion(
  sessionId: string,
  versionId: string,
  body: { action_id: string; expected_state_version: number },
): Promise<ContextActionResponse> {
  return apiFetch<ContextActionResponse>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/artifacts/`
    + `${encodeURIComponent(versionId)}/select`,
    { method: 'POST', body },
  )
}

// 读取指定不可变版本（含来源、基础版本与用量）。
export function getChatArtifact(sessionId: string, versionId: string): Promise<ArtifactVersion> {
  return apiFetch<ArtifactVersion>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(versionId)}`,
  )
}

// 一条 SSE 事件：event 是事件名，data 是那行 JSON 解出来的对象。
export interface ChatStreamEvent {
  event: string
  data: Record<string, unknown>
}

export interface ChatStreamInput {
  text?: string
  /** 一组图片（1–9 张，S7.14 起字段名是 `files`，同属一组） */
  files?: File[] | null
  /** 可选的结构化操作绑定 JSON（v2 §3.3B），服务端校验后随用户消息持久保存 */
  operationBinding?: string
}

// 把一帧 SSE 文本（event 行 + 若干 data 行）解析成事件；注释行（": ping"）与坏帧返回 null。
// 后端每个 data 都是一行 JSON（换行被转义），这里仍按规范把多行 data 拼起来，
// 避免将来后端改成多行时前端悄悄丢内容。
function parseFrame(frame: string): ChatStreamEvent | null {
  let name: string | null = null
  const dataLines: string[] = []
  for (const rawLine of frame.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (line === '' || line.startsWith(':')) continue
    if (line.startsWith('event:')) {
      name = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''))
    }
  }
  if (name === null || dataLines.length === 0) return null
  let data: Record<string, unknown> = {}
  try {
    const parsed: unknown = JSON.parse(dataLines.join('\n'))
    if (parsed !== null && typeof parsed === 'object') data = parsed as Record<string, unknown>
  } catch {
    // 坏帧就当空对象：事件名还在，交给上层兜底，不让整条流断掉
  }
  return { event: name, data }
}

// 发一条消息并逐帧消费 SSE（S7.4 接口）。
// 不用 EventSource：它只支持 GET，而发消息是带 multipart 的 POST；这里用 fetch +
// ReadableStream 自己按空行切帧，也不吃 EventSource 的自动重连（对话轮次不重放）。
// signal 只停「转发」：后端这一轮跑在 detached 任务里，断开照常跑完并落库。
export async function streamMessage(
  sessionId: string,
  input: ChatStreamInput,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const form = new FormData()
  if (input.text) form.append('text', input.text)
  if (input.operationBinding) form.append('operation_binding', input.operationBinding)
  for (const file of input.files ?? []) form.append('files', file)

  const response = await fetch(
    apiUrl(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`),
    { method: 'POST', body: form, signal },
  )
  if (!response.ok) throw await normalizeResponseError(response)
  if (response.body === null) {
    throw new ApiRequestError('当前浏览器不支持流式响应', undefined, response.status)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const flush = (chunk: string): void => {
    buffer += chunk
    let index = buffer.indexOf('\n\n')
    while (index >= 0) {
      const frame = buffer.slice(0, index)
      buffer = buffer.slice(index + 2)
      const parsed = parseFrame(frame)
      if (parsed !== null) onEvent(parsed)
      index = buffer.indexOf('\n\n')
    }
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      flush(decoder.decode(value, { stream: true }))
    }
    flush(decoder.decode())
    const tail = parseFrame(buffer)
    if (tail !== null) onEvent(tail)
  } finally {
    reader.releaseLock()
  }
}
