import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiRequestError } from '@/api/client'
import type { AnalyzeRequest, Element, HotspotClue, ImageClueResult, JobStatus } from '@/api/analysis'
import { getJob, parseImageClue, submitAnalysis } from '@/api/analysis'

/** 分析台的一次提交—轮询流程：idle → submitting → polling → succeeded / failed。 */
export type AnalysisPhase = 'idle' | 'submitting' | 'polling' | 'succeeded' | 'failed'

/** 图片热点解析的状态机（S6.8）：idle → parsing → parsed / failed。 */
export type ImagePhase = 'idle' | 'parsing' | 'parsed' | 'failed'

/** 一个热点输入项：手动输入的只有 `text`；来自图片解析的额外带一份可编辑线索（S6.8）。 */
export interface HotspotInput {
  text: string
  clue?: HotspotClue | null
}

/** 一次提交里、与 `hotspots` 一一对应的线索项：null = 该条走正常拆解。 */
type SubmitItem = { text: string; clue: HotspotClue | null }

const POLL_INTERVAL_MS = 2000

/**
 * 提交前把 `hotspot_raw` 同步成当前文本（数据契约 §3.2：服务端也会用当前原文覆盖一遍，
 * 这里保持两端一致）。契约的 `HotspotClue` 没列出该字段（服务端按 extra="allow" 收下），
 * 所以补一个类型断言，而不是手写并行类型。
 */
function clueWithRawText(clue: HotspotClue, text: string): HotspotClue {
  return { ...clue, hotspot_raw: text } as HotspotClue
}

export const useAnalysisStore = defineStore('analysis', () => {
  // 输入区：hotspots 用对象数组是为了适配 n-dynamic-input（原始类型数组无法双向绑定单个元素）
  const hotspots = ref<HotspotInput[]>([{ text: '' }])
  const topk = ref(5)

  // 图片入口（S6.8）：解析结果只留在内存里，用户确认/编辑后才随 clues 提交，不落库不落盘
  const imagePhase = ref<ImagePhase>('idle')
  const imageFile = ref<File | null>(null)
  const imageError = ref<string | null>(null)
  const imageResult = ref<ImageClueResult | null>(null)
  /** 可编辑的热点描述（初值取解析结果的 `raw_text`） */
  const imageDraftText = ref('')
  /** 解析结果里的要素卡（可增删改）；没有结果时是空数组 */
  const imageElements = computed<Element[]>(() => imageResult.value?.clue.elements ?? [])

  const phase = ref<AnalysisPhase>('idle')
  const job = ref<JobStatus | null>(null)
  const failure = ref<{ message: string; detail?: string } | null>(null)
  /** 提交时刻（毫秒），用于耗时展示 */
  const startedAt = ref<number | null>(null)

  let timer: number | undefined

  const isActive = computed(() => phase.value === 'submitting' || phase.value === 'polling')

  function stopPolling(): void {
    if (timer !== undefined) {
      window.clearInterval(timer)
      timer = undefined
    }
  }

  function describeError(err: unknown): { message: string; detail?: string } {
    if (err instanceof ApiRequestError) {
      return { message: err.message, detail: err.detail === undefined ? undefined : JSON.stringify(err.detail) }
    }
    if (err instanceof Error) {
      return { message: err.message }
    }
    return { message: '未知错误' }
  }

  async function pollOnce(jobId: string): Promise<void> {
    try {
      const status = await getJob(jobId)
      job.value = status
      if (status.status === 'succeeded' || status.status === 'failed') {
        stopPolling()
        phase.value = status.status
        if (status.status === 'failed') {
          const err = status.error
          failure.value = err
            ? { message: err.message, detail: err.detail === undefined || err.detail === null ? undefined : JSON.stringify(err.detail) }
            : { message: '任务失败（后端未返回错误详情）' }
        }
      }
    } catch (err) {
      stopPolling()
      phase.value = 'failed'
      failure.value = describeError(err)
    }
  }

  function startPolling(jobId: string): void {
    stopPolling()
    timer = window.setInterval(() => {
      void pollOnce(jobId)
    }, POLL_INTERVAL_MS)
  }

  /** 提交前的基础校验（契约 AnalyzeRequest：1–10 条、每条 1–500 字、topk 1–20）。 */
  function validateItems(items: SubmitItem[]): string | null {
    if (items.length === 0) return '请至少填写一个热点'
    if (items.length > 10) return '热点最多 10 个'
    if (items.some((item) => item.text.length > 500)) return '单条热点不能超过 500 字'
    if (!Number.isInteger(topk.value) || topk.value < 1 || topk.value > 20) return '候选数 topk 需在 1–20 之间'
    return null
  }

  /** 去掉空白项后，把输入项（可只取一条）整理成提交项；`clue` 缺省补 null。 */
  function toItems(source: HotspotInput[]): SubmitItem[] {
    return source
      .map((item) => ({ text: item.text.trim(), clue: item.clue ?? null }))
      .filter((item) => item.text.length > 0)
  }

  /** 真正发提交请求：任何带 clue 的一次提交都组装出等长的 `clues`（无 clue 的位置补 null）。 */
  async function runSubmit(items: SubmitItem[]): Promise<string | null> {
    stopPolling()
    phase.value = 'submitting'
    job.value = null
    failure.value = null
    startedAt.value = Date.now()

    const payload: AnalyzeRequest = { hotspots: items.map((item) => item.text), topk: topk.value }
    // 一条 clue 都没有时不发 `clues` 字段：与 S6.7 之前的请求体逐字节一致
    if (items.some((item) => item.clue !== null)) {
      payload.clues = items.map((item) =>
        item.clue === null ? null : clueWithRawText(item.clue, item.text),
      )
    }

    try {
      const accepted = await submitAnalysis(payload)
      job.value = { job_id: accepted.job_id, run_id: accepted.run_id, status: 'queued', progress: 0 }
      phase.value = 'polling'
      startPolling(accepted.job_id)
      return null
    } catch (err) {
      phase.value = 'failed'
      failure.value = describeError(err)
      return null
    }
  }

  /**
   * 提交分析。`only` 给下标时只提交那一条（图片卡片的「用这条热点分析」，其余输入保持不动）。
   */
  async function submit(options: { only?: number } = {}): Promise<string | null> {
    const source = options.only === undefined
      ? hotspots.value
      : hotspots.value.slice(options.only, options.only + 1)
    const items = toItems(source)
    const invalid = validateItems(items)
    if (invalid !== null) return invalid
    return runSubmit(items)
  }

  // ---------- 图片热点解析（S6.8） ----------

  async function applyParse(file: File): Promise<void> {
    imageFile.value = file
    imagePhase.value = 'parsing'
    imageError.value = null
    try {
      const result = await parseImageClue(file)
      imageResult.value = result
      imageDraftText.value = result.raw_text
      imagePhase.value = 'parsed'
    } catch (err) {
      imageResult.value = null
      imageDraftText.value = ''
      imagePhase.value = 'failed'
      imageError.value = describeError(err).message
    }
  }

  /** 解析一张图片（选择文件 / 拖拽 / 粘贴都走这里）。失败不重试，由用户点「重解析」。 */
  function parseImage(file: File): Promise<void> {
    return applyParse(file)
  }

  /** 重解析：复用内存里那份 File，不再让用户选一次。 */
  function reparse(): Promise<void> {
    if (imageFile.value === null) return Promise.resolve()
    return applyParse(imageFile.value)
  }

  /** 丢弃解析结果与图片，回到 idle（下一次重新选图）。 */
  function discardImage(): void {
    imagePhase.value = 'idle'
    imageFile.value = null
    imageError.value = null
    imageResult.value = null
    imageDraftText.value = ''
  }

  function updateElement(index: number, patch: Partial<Element>): void {
    const element = imageResult.value?.clue.elements[index]
    if (element === undefined) return
    Object.assign(element, patch)
  }

  function addElement(): void {
    imageResult.value?.clue.elements.push({ type: 'topic', value: '', weight: 0.6, confidence: 0.7 })
  }

  function removeElement(index: number): void {
    imageResult.value?.clue.elements.splice(index, 1)
  }

  /**
   * 把图片解析结果并进热点列表（可与手动输入的热点混在同一次批量提交里），成功后清掉图片卡片。
   * 返回 null 表示成功，否则是要给用户看的校验提示。
   */
  function addImageHotspot(): string | null {
    if (imageResult.value === null) return '请先解析图片'
    const text = imageDraftText.value.trim()
    if (text.length === 0) return '热点描述不能为空'
    if (toItems(hotspots.value).length >= 10) return '热点最多 10 个'
    hotspots.value.push({ text, clue: imageResult.value.clue })
    discardImage()
    return null
  }

  /** 「用这条热点分析」：只提交当前图片这一条，不并入列表。 */
  async function submitOnly(): Promise<string | null> {
    if (imageResult.value === null) return '请先解析图片'
    const text = imageDraftText.value.trim()
    if (text.length === 0) return '热点描述不能为空'
    const items: SubmitItem[] = [{ text, clue: imageResult.value.clue }]
    const invalid = validateItems(items)
    if (invalid !== null) return invalid
    return runSubmit(items)
  }

  function reset(): void {
    stopPolling()
    phase.value = 'idle'
    job.value = null
    failure.value = null
    startedAt.value = null
  }

  return {
    hotspots, topk, phase, job, failure, startedAt, isActive, submit, reset,
    imagePhase, imageFile, imageError, imageResult, imageDraftText, imageElements,
    parseImage, reparse, discardImage, addImageHotspot, submitOnly,
    updateElement, addElement, removeElement,
  }
})
