<script setup lang="ts">
/**
 * 图片入口（S7.17）：选择 / 拖拽 / 粘贴三个入口的**唯一实现**，缩略图与逐张删除也在这一处。
 *
 * 两边靠 `max` 区分：分析台 `max = 1`（`thumbs = false`，与 S6.8 的视觉逐条一致），
 * 对话页 `max = 9`（多图热点，S7.14/S7.18）。
 *
 * 校验口径与后端一致，但**服务端仍是权威**（客户端只是为了不白跑一次请求）：
 * 白名单 jpeg / png / webp、单张 ≤ `maxSizeMb`、总数 ≤ `max`、合计 ≤ `maxTotalMb`；
 * 任何一条不满足都**整批拒绝**并给一句能直接显示的话，不静默截断、不悄悄丢掉某一张。
 *
 * 粘贴只在剪贴板**含图片**时接管（纯文本一律放行，否则会吃掉正常粘贴）；挂在最外层容器上
 * 靠原生事件冒泡收（Naive UI 的 `n-input` 不透传原生事件）。截图没有文件名时生成
 * `粘贴图片-N.<ext>`，保证附件说明与缩略图可读。
 */
import { NButton, NTag } from 'naive-ui'
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  /** 当前选中的文件（`v-model:files`） */
  files: File[]
  /** 张数上限 */
  max?: number
  /** 单张体积上限（MB） */
  maxSizeMb?: number
  /** 合计体积上限（MB） */
  maxTotalMb?: number
  /** 是否显示缩略图列表（分析台单图场景关掉，保持原有视觉） */
  thumbs?: boolean
  /** panel = 虚线面板（分析台 / 旧版）；composer = 只留隐藏 file input，UI 交给 ChatView */
  variant?: 'panel' | 'composer'
  disabled?: boolean
  /** 标题与提示文案（两处页面各自措辞不同） */
  title?: string
  hint?: string
  label?: string
}>(), {
  max: 1,
  maxSizeMb: 10,
  maxTotalMb: 30,
  thumbs: true,
  variant: 'panel',
  disabled: false,
  title: '',
  hint: '',
  label: '',
})

const emit = defineEmits<{
  (event: 'update:files', value: File[]): void
  /** 校验未通过时的提示；传空串表示清掉上一次的提示 */
  (event: 'error', message: string): void
}>()

const ACCEPT = 'image/jpeg,image/png,image/webp'
const MIME_SUFFIX: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
}

const input = ref<HTMLInputElement | null>(null)
const dragging = ref(false)
const previews = ref<string[]>([])

const titleText = computed(() => props.title || (props.max > 1 ? '丢几张图，合成一条热点' : '丢一张图，自动拆成热点标签'))
const labelText = computed(() => props.label || (props.max > 1 ? '选择图片（可多选）' : '选择图片'))

/** 缩略图用 object URL；每次列表变化重建，卸载时全部释放（否则浏览器会一直占着内存） */
watch(() => props.files, (files) => {
  previews.value.forEach((url) => URL.revokeObjectURL(url))
  previews.value = files.map((file) => URL.createObjectURL(file))
}, { immediate: true })

onBeforeUnmount(() => {
  previews.value.forEach((url) => URL.revokeObjectURL(url))
  previews.value = []
})

function pick(): void {
  if (props.disabled) return
  input.value?.click()
}

function onFileChange(event: Event): void {
  const element = event.target as HTMLInputElement
  const chosen = Array.from(element.files ?? [])
  element.value = ''                 // 清空后才能连续选同一个文件
  addFiles(chosen)
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  if (props.disabled) return
  const dropped = Array.from(event.dataTransfer?.files ?? [])
  if (dropped.length === 0) return
  addFiles(dropped)
}

function handlePaste(event: ClipboardEvent): void {
  const items = Array.from(event.clipboardData?.items ?? [])
  const hasImage = items.some((item) => item.kind === 'file' && item.type.startsWith('image/'))
  // 禁用态只拦图片，纯文本照常放行（不能吃掉用户正常粘贴文字）
  if (props.disabled) {
    if (hasImage) event.preventDefault()
    return
  }
  const pasted = items
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null)
  if (pasted.length === 0) return    // 纯文本：放行，绝不 preventDefault
  event.preventDefault()
  addFiles(pasted, { pasted: true })
}

function removeAt(index: number): void {
  if (props.disabled) return
  emit('update:files', props.files.filter((_file, position) => position !== index))
}

/** 粘贴来的图常常没有文件名（截图）：补一个可读名字 */
function namedForPaste(file: File, index: number): File {
  if (file.name) return file
  const suffix = MIME_SUFFIX[file.type] ?? 'png'
  return new File([file], `粘贴图片-${index}.${suffix}`, { type: file.type })
}

function addFiles(incoming: File[], options: { pasted?: boolean } = {}): void {
  if (props.disabled) return
  if (incoming.length === 0) return
  // 任一非法文件即整批拒绝：不能先静默过滤空文件再收下同批其他文件。
  const empty = incoming.findIndex((file) => !file || file.size === 0)
  if (empty >= 0) {
    emit('error', `第 ${empty + 1} 张是空文件，这一批没有加入`)
    return
  }
  const files = incoming
  const badType = files.findIndex((file) => !MIME_SUFFIX[file.type])
  if (badType >= 0) {
    emit('error', `第 ${badType + 1} 张不是 jpg / png / webp 图片`)
    return
  }
  const tooBig = files.findIndex((file) => file.size > props.maxSizeMb * 1024 * 1024)
  if (tooBig >= 0) {
    emit('error', `第 ${tooBig + 1} 张超过 ${props.maxSizeMb} MB 上限`)
    return
  }
  if (props.files.length + files.length > props.max) {
    emit('error', `最多选 ${props.max} 张图片（这次收到 ${files.length} 张）`)
    return
  }
  const merged = [
    ...props.files,
    ...files.map((file, index) =>
      options.pasted ? namedForPaste(file, props.files.length + index + 1) : file),
  ]
  const totalBytes = merged.reduce((sum, file) => sum + file.size, 0)
  if (totalBytes > props.maxTotalMb * 1024 * 1024) {
    emit('error', `这些图片合计超过 ${props.maxTotalMb} MB 上限`)
    return
  }
  emit('error', '')                  // 通过校验：清掉上一次的提示
  emit('update:files', merged)
}

// composer 变体由 ChatView 驱动：校验 / 命名 / object URL 生命周期仍只在这一处。
defineExpose({ pick, addFiles, handlePaste, previews })
</script>

<template>
  <!-- composer 变体只保留隐藏的 file input，不渲染虚线面板（分析台仍走 panel，零改动） -->
  <div v-if="variant === 'composer'" class="hidden">
    <input
      ref="input"
      type="file"
      :accept="ACCEPT"
      :multiple="max > 1"
      :disabled="disabled"
      class="hidden"
      @change="onFileChange"
    >
  </div>
  <div v-else class="flex flex-col gap-2">
    <div
      class="flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-6 text-center"
      :class="dragging ? 'border-brand bg-surface' : 'border-line bg-surface'"
      :tabindex="disabled ? -1 : 0"
      role="button"
      :aria-disabled="disabled"
      @click="pick"
      @keydown.enter.prevent="pick"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
      @paste="handlePaste"
    >
      <p class="text-sm font-medium text-ink-strong">{{ titleText }}</p>
      <p v-if="hint" class="text-xs text-ink-muted">{{ hint }}</p>
      <n-button size="small" :disabled="disabled" @click.stop="pick">
        {{ labelText }}
      </n-button>
      <input
        ref="input"
        type="file"
        :accept="ACCEPT"
        :multiple="max > 1"
        :disabled="disabled"
        class="hidden"
        @change="onFileChange"
      >
    </div>

    <div v-if="thumbs && files.length > 0" class="flex flex-wrap gap-2">
      <n-tag
        v-for="(file, index) in files"
        :key="`${file.name}-${index}`"
        size="small"
        closable
        @close="removeAt(index)"
      >
        <img
          v-if="previews[index]"
          :src="previews[index]"
          alt=""
          class="mr-1 inline-block h-5 w-5 rounded object-cover align-middle"
        >
        {{ file.name }}
      </n-tag>
    </div>
  </div>
</template>
