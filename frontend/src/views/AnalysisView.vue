<script setup lang="ts">
import {
  NAlert, NButton, NCard, NDynamicInput, NInput, NInputNumber, NProgress, NSelect, NSpin, NTag,
} from 'naive-ui'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import type { Element } from '@/api/analysis'
import { ELEMENT_TYPE_LABELS, ELEMENT_TYPES } from '@/api/analysis'
import { useAnalysisStore } from '@/stores/analysis'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const analysisStore = useAnalysisStore()
const router = useRouter()

const phase = computed(() => analysisStore.phase)
const job = computed(() => analysisStore.job)

const elapsedSec = ref(0)
let elapsedTimer: number | undefined

watch(phase, (value) => {
  if (value === 'submitting' || value === 'polling') {
    elapsedSec.value = 0
    elapsedTimer = window.setInterval(() => {
      elapsedSec.value += 1
    }, 1000)
  } else if (elapsedTimer !== undefined) {
    window.clearInterval(elapsedTimer)
    elapsedTimer = undefined
  }
})

// 成功后自动跳转结果页（结果详情 S5.4 丰满本页）
watch(phase, (value) => {
  if (value === 'succeeded' && analysisStore.job) {
    void router.push({ name: 'run-result', params: { runId: analysisStore.job.run_id } })
  }
})

onBeforeUnmount(() => {
  // 轮询由 store 持有的 timer 驱动，页面卸载不停止；仅清本地计时器
  if (elapsedTimer !== undefined) {
    window.clearInterval(elapsedTimer)
    elapsedTimer = undefined
  }
})

const statusMeta = computed(() => {
  switch (job.value?.status) {
    case 'queued':
      return { type: 'warning' as const, label: '排队中' }
    case 'running':
      return { type: 'info' as const, label: '分析中' }
    case 'succeeded':
      return { type: 'success' as const, label: '已完成' }
    case 'failed':
      return { type: 'error' as const, label: '失败' }
    default:
      return null
  }
})

const progressPercent = computed(() => Math.round((job.value?.progress ?? 0) * 100))

/** 排队超过 30 秒多半是 worker 没有消费，给出排查提示 */
const queuedTooLong = computed(
  () => job.value?.status === 'queued' && elapsedSec.value > 30,
)

const inputError = ref<string | null>(null)

async function onSubmit(): Promise<void> {
  inputError.value = await analysisStore.submit()
}

// ---------- 从图片拆热点（S6.8） ----------

/** 要素类型下拉：九类的中文标签（口径见产品方案 §六），与结果页共用同一份映射。 */
const elementTypeOptions = ELEMENT_TYPES.map((type) => ({ label: ELEMENT_TYPE_LABELS[type], value: type }))

const imagePhase = computed(() => analysisStore.imagePhase)
const imageResult = computed(() => analysisStore.imageResult)

const fileInput = ref<HTMLInputElement | null>(null)
const dragging = ref(false)
const imageError = ref<string | null>(null)

function pickImage(): void {
  fileInput.value?.click()
}

/** 选择文件 / 拖拽 / 粘贴三个入口都汇到 store.parseImage；不重试，失败由用户点「重解析」。 */
function startParse(file: File): void {
  imageError.value = null
  void analysisStore.parseImage(file)
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''            // 清空后可连续选同一个文件
  if (file) startParse(file)
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  const file = event.dataTransfer?.files?.[0]
  if (file) startParse(file)
}

function onPaste(event: ClipboardEvent): void {
  const file = event.clipboardData?.files?.[0]
  if (!file) return
  event.preventDefault()
  startParse(file)
}

function onReparse(): void {
  imageError.value = null
  void analysisStore.reparse()
}

function onDiscardImage(): void {
  imageError.value = null
  analysisStore.discardImage()
}

function onAddImageHotspot(): void {
  imageError.value = analysisStore.addImageHotspot()
}

async function onSubmitOnly(): Promise<void> {
  imageError.value = await analysisStore.submitOnly()
}

function setElementType(index: number, value: string | number | Array<string | number> | null): void {
  if (typeof value === 'string') analysisStore.updateElement(index, { type: value as Element['type'] })
}

function setElementValue(index: number, value: string): void {
  analysisStore.updateElement(index, { value })
}

function setElementWeight(index: number, value: number | null): void {
  if (value !== null) analysisStore.updateElement(index, { weight: value })
}
</script>

<template>
  <main class="px-4 py-8">
    <div class="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <!-- 页面入口统一由顶栏导航提供（S5.7），此页不再放重复按钮 -->
      <header>
        <h1 class="text-2xl font-bold text-ink-strong">
          {{ appStore.title }}
        </h1>
        <p class="mt-1 text-sm text-ink">
          输入一个或多个热点（也可以丢一张图自动拆），在你的素材库里检索可蹭的素材并产出文案初稿
          （1–10 个热点，逐个分析）。
        </p>
      </header>

      <n-card title="分析台">
        <div class="flex flex-col gap-4">
          <div>
            <div class="mb-2 text-sm font-medium text-ink-strong">
              热点文本
            </div>
            <n-dynamic-input
              v-model:value="analysisStore.hotspots"
              :min="1"
              :max="10"
              :on-create="() => ({ text: '' })"
            >
              <template #default="{ value }">
                <div class="flex w-full items-start gap-2">
                  <n-input
                    v-model:value="value.text"
                    type="textarea"
                    :autosize="{ minRows: 1, maxRows: 3 }"
                    maxlength="500"
                    placeholder="例如：某明星打羽毛球被拍，反差感拉满"
                  />
                  <n-tag v-if="value.clue" size="small" type="info" class="mt-2 shrink-0">
                    来自图片
                  </n-tag>
                </div>
              </template>
            </n-dynamic-input>
          </div>

          <div class="flex items-center gap-3">
            <span class="text-sm font-medium text-ink-strong">每热点候选数 topk</span>
            <n-input-number v-model:value="analysisStore.topk" :min="1" :max="20" class="w-28" />
          </div>

          <div class="flex items-center gap-3">
            <n-button
              type="primary"
              :loading="phase === 'submitting'"
              :disabled="analysisStore.isActive"
              @click="onSubmit"
            >
              提交分析
            </n-button>
            <n-button v-if="phase === 'failed'" @click="analysisStore.reset()">
              重置
            </n-button>
          </div>

          <n-alert v-if="inputError" type="error" :title="inputError" />
          <n-alert
            v-if="analysisStore.failure"
            type="error"
            :title="analysisStore.failure.message"
          >
            <pre v-if="analysisStore.failure.detail" class="whitespace-pre-wrap text-xs">{{ analysisStore.failure.detail }}</pre>
          </n-alert>

          <div v-if="phase === 'polling' || phase === 'succeeded'" class="flex flex-col gap-2 rounded-lg border border-line bg-surface p-4">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <n-tag v-if="statusMeta" :type="statusMeta.type" size="small">
                  {{ statusMeta.label }}
                </n-tag>
                <span v-if="job?.current_step" class="text-sm text-ink">
                  当前步骤：{{ job.current_step }}
                </span>
              </div>
              <span class="text-xs text-ink-muted">已用 {{ elapsedSec }} 秒</span>
            </div>
            <n-progress type="line" :percentage="progressPercent" :height="10" />
            <p class="text-xs text-ink-muted">
              job_id：<code class="rounded bg-surface-muted px-1">{{ job?.job_id }}</code>
            </p>
            <n-alert v-if="queuedTooLong" type="warning" title="任务仍在排队">
              已排队超过 30 秒。请确认 Celery worker 已启动：
              <code class="rounded bg-surface-muted px-1">uv run celery -A xhs_agent.tasks.worker:app worker --loglevel=info</code>
            </n-alert>
          </div>
        </div>
      </n-card>

      <!-- 图片入口（S6.8）：解析结果只留在内存里，用户确认/编辑后才随 clues 提交 -->
      <n-card title="从图片拆热点">
        <div class="flex flex-col gap-4">
          <div
            class="flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-6 text-center"
            :class="dragging ? 'border-brand bg-surface' : 'border-line bg-surface'"
            tabindex="0"
            role="button"
            @click="pickImage"
            @keydown.enter.prevent="pickImage"
            @dragover.prevent="dragging = true"
            @dragleave.prevent="dragging = false"
            @drop.prevent="onDrop"
            @paste="onPaste"
          >
            <p class="text-sm font-medium text-ink-strong">
              丢一张图，自动拆成热点标签
            </p>
            <p class="text-xs text-ink-muted">
              点这里选文件、把图片拖进来，或先点一下这里再 Ctrl/⌘+V 粘贴（jpg / png / webp，单个 ≤ 10 MB）
            </p>
            <n-button size="small" :loading="imagePhase === 'parsing'" @click.stop="pickImage">
              选择图片
            </n-button>
            <input
              ref="fileInput"
              type="file"
              accept="image/jpeg,image/png,image/webp"
              class="hidden"
              @change="onFileChange"
            >
          </div>

          <div v-if="imagePhase === 'parsing'" class="flex items-center gap-2 text-sm text-ink">
            <n-spin size="small" />
            正在解析图片…
          </div>

          <n-alert
            v-if="imagePhase === 'failed'"
            type="error"
            :title="analysisStore.imageError ?? '图片解析失败'"
          />

          <div
            v-if="imagePhase === 'parsed' && imageResult"
            class="flex flex-col gap-4 rounded-lg border border-line bg-surface-muted p-4"
          >
            <div>
              <div class="mb-2 text-sm font-medium text-ink-strong">
                热点描述（可编辑）
              </div>
              <n-input
                v-model:value="analysisStore.imageDraftText"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 5 }"
                maxlength="500"
                show-count
                placeholder="模型给的热点描述，可以直接改"
              />
            </div>

            <div>
              <div class="mb-2 flex items-center justify-between">
                <span class="text-sm font-medium text-ink-strong">爆点要素（类型 / 取值 / 权重可改）</span>
                <n-button size="small" quaternary @click="analysisStore.addElement()">
                  添加要素
                </n-button>
              </div>
              <p v-if="analysisStore.imageElements.length === 0" class="text-sm text-ink-muted">
                未识别到要素
              </p>
              <div
                v-for="(element, index) in analysisStore.imageElements"
                :key="index"
                class="mb-2 flex flex-wrap items-center gap-2"
              >
                <n-select
                  :value="element.type"
                  :options="elementTypeOptions"
                  size="small"
                  class="w-28"
                  @update:value="(value) => setElementType(index, value)"
                />
                <n-input
                  :value="element.value"
                  size="small"
                  class="min-w-40 flex-1"
                  placeholder="要素取值"
                  @update:value="(value) => setElementValue(index, value)"
                />
                <n-input-number
                  :value="element.weight"
                  :min="0"
                  :max="1"
                  :step="0.1"
                  size="small"
                  class="w-24"
                  @update:value="(value) => setElementWeight(index, value)"
                />
                <n-button size="small" quaternary @click="analysisStore.removeElement(index)">
                  删除
                </n-button>
              </div>
            </div>

            <!-- 机制 / 借势角度 / 风险提示只读：模型给的判断，改的是要素卡 -->
            <div class="flex flex-col gap-2 text-sm">
              <div v-if="imageResult.clue.why_it_works.length > 0">
                <span class="text-ink-strong">为什么有效：</span>
                <span class="text-ink">{{ imageResult.clue.why_it_works.join('；') }}</span>
              </div>
              <div v-if="imageResult.clue.mechanisms.length > 0">
                <span class="text-ink-strong">机制：</span>
                <span class="text-ink">
                  {{ imageResult.clue.mechanisms.map((m) => m.explain ? `${m.name}（${m.explain}）` : m.name).join('；') }}
                </span>
              </div>
              <div v-if="imageResult.clue.borrow_angles && imageResult.clue.borrow_angles.length > 0">
                <span class="text-ink-strong">借势角度：</span>
                <span class="text-ink">{{ imageResult.clue.borrow_angles.join('；') }}</span>
              </div>
              <div v-if="imageResult.clue.risk_notes && imageResult.clue.risk_notes.length > 0">
                <span class="text-ink-strong">风险提示：</span>
                <span class="text-ink">{{ imageResult.clue.risk_notes.join('；') }}</span>
              </div>
            </div>

            <div class="flex flex-wrap items-center gap-3">
              <n-button size="small" @click="onReparse">
                重解析
              </n-button>
              <n-button size="small" @click="onDiscardImage">
                丢弃
              </n-button>
              <n-button size="small" @click="onAddImageHotspot">
                加入热点列表
              </n-button>
              <n-button type="primary" size="small" @click="onSubmitOnly">
                用这条热点分析
              </n-button>
            </div>
          </div>

          <n-alert v-if="imageError" type="error" :title="imageError" />
        </div>
      </n-card>
    </div>
  </main>
</template>
