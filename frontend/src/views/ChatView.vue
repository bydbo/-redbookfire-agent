<script setup lang="ts">
// 对话首页（S7.5）：传一张图 + 一句话 → 模型自己决定要不要调「跑完整分析」→ 过程流式可见 →
// 结果卡片回到对话里。刷新后会话与消息仍在（库里读回来），运行中的那轮靠轮询补齐。
import { NAlert, NButton, NCard, NEmpty, NInput, NSpin, NTag } from 'naive-ui'
import { nextTick, onMounted, ref, watch } from 'vue'

import type { ChatMessage } from '@/api/chat'
import HotspotResult from '@/components/HotspotResult.vue'
import { useChatStore } from '@/stores/chat'

const store = useChatStore()

const draft = ref('')
const picked = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const messagesEl = ref<HTMLElement | null>(null)

/** 贴底判据：距底 40px 以内算「用户还在底部」，此时新消息/流式增量才自动跟随。 */
const STICK_THRESHOLD_PX = 40
const stickToBottom = ref(true)

function nearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX
}

function onMessagesScroll(): void {
  const el = messagesEl.value
  if (el !== null) stickToBottom.value = nearBottom(el)
}

/** 滚到最新一条；`force` 用于「打开会话 / 首次进入」这种必须贴底的场景。 */
async function scrollToLatest(force = false): Promise<void> {
  if (!force && !stickToBottom.value) return
  await nextTick()
  const el = messagesEl.value
  if (el === null) return
  el.scrollTop = el.scrollHeight
  stickToBottom.value = true
}

// 新消息 / 流式增量 / 工具步骤变化：只有贴底时才跟随——用户上翻读历史时不要拽回去
watch(
  [() => store.messages.length, () => store.streamText, () => store.toolSteps.length],
  () => {
    void scrollToLatest()
  },
)

// 切换（或首次打开）会话：直接跳到底，语义是「看最新的」
watch(() => store.currentId, () => {
  void scrollToLatest(true)
})

onMounted(async () => {
  await store.init()
  await scrollToLatest(true)
})

// 消息里带 run_id 的结果卡片：拿到 id 就把 run 取回来（store 里缓存，失败只记 null）
watch(() => store.messages, (items) => {
  for (const item of items) {
    if (item.run_id) void store.ensureRun(item.run_id)
  }
}, { immediate: true })

function pickFile(): void {
  fileInput.value?.click()
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  picked.value = input.files?.[0] ?? null
  input.value = ''
}

function clearFile(): void {
  picked.value = null
}

async function submit(): Promise<void> {
  const text = draft.value
  const file = picked.value
  draft.value = ''
  picked.value = null
  const invalid = await store.send({ text, file })
  if (invalid !== null) {
    draft.value = text
    picked.value = file
  }
}

function messageText(message: ChatMessage): string {
  if (message.status === 'running') return store.streamText
  return message.content
}

function statusLabel(message: ChatMessage): string | null {
  switch (message.status) {
    case 'running':
      return '正在回复…'
    case 'failed':
      return '这一轮失败了'
    case 'interrupted':
      return '这一轮没跑完就断了'
    default:
      return null
  }
}

function sessionLabel(item: { title: string; session_id: string }): string {
  return item.title || '新对话'
}
</script>

<template>
  <!-- 对话页自己锁高度：外壳给的是 min-h-0 flex-1，这里 h-full 撑满 → 全页只有消息区滚动 -->
  <main class="h-full min-h-0 px-4 py-6">
    <div class="mx-auto flex h-full min-h-0 w-full max-w-5xl flex-col gap-4">
      <header class="flex shrink-0 flex-wrap items-end justify-between gap-3">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">
            热点搭子
          </h1>
          <p class="mt-1 text-sm text-ink">
            丢一张图或一句话过来，我帮你把热点拆开、找素材、写初稿（会自己决定要不要跑完整分析）。
          </p>
        </div>
        <n-button size="small" :disabled="store.busy" @click="store.newSession()">
          新对话
        </n-button>
      </header>

      <n-alert v-if="store.error" class="shrink-0" type="error" :title="store.error" />

      <div class="flex min-h-0 flex-1 flex-col gap-4 md:flex-row md:gap-6">
        <!-- 历史会话侧栏：消息区之外，自己滚；窄屏限高，别把消息区挤没 -->
        <aside class="w-full shrink-0 md:w-56">
          <n-card title="历史会话" size="small">
            <div class="chat-sessions-scroll max-h-28 overflow-y-auto md:max-h-80">
              <div v-if="store.sessionsLoading" class="text-sm text-ink-muted">
                加载中…
              </div>
              <n-alert v-else-if="store.sessionsError" type="warning" :title="store.sessionsError" />
              <n-empty v-else-if="store.sessions.length === 0" description="还没有会话" />
              <ul v-else class="flex flex-col gap-1">
                <li v-for="item in store.sessions" :key="item.session_id">
                  <button
                    type="button"
                    class="chat-session-item w-full rounded-lg px-2 py-1 text-left text-sm"
                    :class="{ 'is-active': item.session_id === store.currentId }"
                    @click="store.openSession(item.session_id)"
                  >
                    <span class="line-clamp-1 font-medium">{{ sessionLabel(item) }}</span>
                    <span class="mt-0.5 line-clamp-1 block text-xs text-ink-muted">
                      {{ item.message_count }} 条 · {{ item.last_message_preview }}
                    </span>
                  </button>
                </li>
              </ul>
            </div>
          </n-card>
        </aside>

        <section class="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
          <!-- 唯一滚动容器：贴底判据与自动跟随都挂在它身上 -->
          <div
            ref="messagesEl"
            class="chat-messages flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1"
            @scroll.passive="onMessagesScroll"
          >
            <n-empty
              v-if="store.messages.length === 0 && !store.busy"
              description="还没有消息，说一句话或丢张图试试"
              class="py-10"
            >
              <template #extra>
                <span class="text-sm text-ink-muted">
                  例：「帮我蹭一下这张图里的热点」+ 一张热点截图
                </span>
              </template>
            </n-empty>

            <div v-for="message in store.messages" :key="message.message_id" class="flex flex-col gap-2">
              <div
                class="max-w-full rounded-2xl px-4 py-3 text-sm"
                :class="message.role === 'user'
                  ? 'ml-auto bg-brand text-white'
                  : 'mr-auto border border-line bg-surface'"
              >
                <p v-if="messageText(message)" class="whitespace-pre-wrap text-ink-strong">
                  {{ messageText(message) }}
                </p>
                <div v-if="message.attachments.length > 0" class="mt-2 flex flex-wrap gap-2">
                  <img
                    v-for="(attachment, index) in message.attachments"
                    :key="attachment.url"
                    :src="attachment.url"
                    :alt="attachment.name || `附件 ${index + 1}`"
                    class="h-20 w-20 rounded-lg object-cover"
                  >
                </div>

                <!-- 工具过程：本轮流式中的粗粒度进度 -->
                <div
                  v-if="message.status === 'running' && store.toolSteps.length > 0"
                  class="mt-2 flex flex-col gap-1 border-t border-line pt-2 text-xs text-ink-muted"
                >
                  <span v-for="(step, index) in store.toolSteps" :key="`${index}-${step.stage}`">
                    · {{ step.stage }}<template v-if="step.total > 0">（{{ step.done }}/{{ step.total }}）</template>
                  </span>
                </div>

                <div v-if="message.status === 'running'" class="mt-2 flex items-center gap-2 text-xs text-ink-muted">
                  <n-spin size="small" />
                  <span>{{ statusLabel(message) }}</span>
                </div>
                <n-alert
                  v-else-if="message.status !== 'succeeded'"
                  class="mt-2"
                  type="warning"
                  :title="message.error || statusLabel(message) || '这一轮没有结果'"
                />
              </div>

              <!-- 持久化的工具过程（刷新后回看） -->
              <div v-if="message.tool_calls.length > 0" class="mr-auto flex flex-wrap gap-1">
                <n-tag
                  v-for="call in message.tool_calls"
                  :key="`${call.tool}-${call.run_id ?? ''}`"
                  size="tiny"
                  :type="call.status === 'succeeded' ? 'success' : 'warning'"
                >
                  {{ store.toolLabel(call.tool) }} ·
                  {{ call.status === 'succeeded' ? '已跑完' : '失败了' }}
                </n-tag>
              </div>

              <!-- 结果卡片：和结果页共用 `HotspotResult` -->
              <template v-if="message.run_id && store.runs[message.run_id]">
                <n-card
                  v-for="(hotspot, index) in store.runs[message.run_id]?.hotspots ?? []"
                  :key="hotspot.hotspot_id"
                  :title="`热点 ${index + 1}`"
                  size="small"
                >
                  <p class="mb-3 text-sm font-medium text-ink-strong">
                    {{ hotspot.hotspot_raw }}
                  </p>
                  <HotspotResult :hotspot="hotspot" compact />
                </n-card>
              </template>
              <div
                v-else-if="message.run_id"
                class="mr-auto text-xs text-ink-muted"
              >
                结果卡片加载中（run_id：{{ message.run_id }}）
              </div>
            </div>
          </div>

          <!-- 输入区：一句话 +（可选）一张图 -->
          <n-card size="small" class="shrink-0">
            <div class="flex flex-col gap-3">
              <n-input
                v-model:value="draft"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 6 }"
                placeholder="想蹭哪条热点？说一句话就行（也可以只丢一张图）"
                :disabled="store.busy"
                @keydown.enter.exact.prevent="submit"
              />
              <div class="flex flex-wrap items-center gap-2">
                <input
                  ref="fileInput"
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  class="hidden"
                  @change="onFileChange"
                >
                <n-button size="small" :disabled="store.busy" @click="pickFile">
                  选一张图
                </n-button>
                <n-tag v-if="picked" size="small" closable @close="clearFile">
                  {{ picked.name }}
                </n-tag>
                <n-button
                  class="ml-auto"
                  size="small"
                  type="primary"
                  :loading="store.busy"
                  :disabled="store.busy"
                  @click="submit"
                >
                  发送
                </n-button>
              </div>
              <p class="text-xs text-ink-muted">
                图片只用于这一次解析（不落盘到素材库）；离开页面这一轮也会跑完，回来看历史就行。
              </p>
            </div>
          </n-card>
        </section>
      </div>
    </div>
  </main>
</template>
