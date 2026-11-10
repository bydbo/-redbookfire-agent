<script setup lang="ts">
/** 左侧导航壳层（S7.30）：品牌 + 新会话 + 三项导航 + 常驻历史会话列表 + 主题切换。
 *
 *  只调 store 的 newSession / loadSessions / openSession / removeSession / renameSession，
 *  不调 init()（挂在壳层会让素材库页也被切会话）。列表数据由 ChatView 与侧栏共用同一 store。
 */
import { NButton, NEmpty, NInput, NTooltip, useMessage } from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { ChatSessionSummary } from '@/api/chat'
import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'

const props = withDefaults(defineProps<{ variant?: 'sidebar' | 'drawer' }>(), {
  variant: 'sidebar',
})
const emit = defineEmits<{ (event: 'navigate'): void }>()

const store = useChatStore()
const appStore = useAppStore()
const route = useRoute()
const router = useRouter()
const message = useMessage()

/** 结果详情也归属「运行历史」这一栏（与原 App.vue 的行为一致）。 */
const activeNav = computed(() => route.name === 'run-result' ? 'run-history' : route.name)
const navItems = [
  { name: 'chat', label: '对话', icon: '◇' },
  { name: 'materials', label: '素材库', icon: '▤' },
  { name: 'run-history', label: '运行历史', icon: '◷' },
]

// 行内重命名状态机：编辑版本 + saving 在途守卫，保证 Enter / blur / Esc / IME 组合
// 全链路最多一次 PATCH，且迟到的响应不覆盖另一次编辑。
const editingId = ref<string | null>(null)
const editingTitle = ref('')
const saving = ref(false)
let editVersion = 0

function cancelEdit(): void {
  editVersion += 1
  editingId.value = null
  editingTitle.value = ''
  saving.value = false
}

async function startEdit(item: ChatSessionSummary): Promise<void> {
  if (saving.value) return
  editVersion += 1
  editingId.value = item.session_id
  editingTitle.value = item.title || '新对话'
  saving.value = false
  await nextTick()
  const input = document.querySelector<HTMLInputElement>('.session-rename-input input')
  input?.focus()
  input?.select()
}

async function saveEdit(): Promise<void> {
  const id = editingId.value
  if (id === null || saving.value) return
  const title = editingTitle.value.trim()
  if (title === '') {
    message.warning('名称不能为空')
    return
  }
  const original = store.sessions.find((item) => item.session_id === id)?.title ?? ''
  if (title === original) {
    cancelEdit()
    return
  }
  const version = editVersion
  saving.value = true
  const error = await store.renameSession(id, title)
  saving.value = false
  if (version !== editVersion) return   // Esc / 切行 / 删除 / 卸载后的迟到响应
  if (error !== null) {
    message.error(error)
    return
  }
  cancelEdit()
}

function onEditKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    cancelEdit()
    return
  }
  if (event.key !== 'Enter' || event.isComposing) return
  event.preventDefault()
  void saveEdit()
}

async function openItem(item: ChatSessionSummary): Promise<void> {
  if (editingId.value !== null) return
  await store.openSession(item.session_id)
  await router.push({ name: 'chat' })
  emit('navigate')
}

async function removeItem(item: ChatSessionSummary): Promise<void> {
  if (editingId.value === item.session_id) cancelEdit()
  const error = await store.removeSession(item.session_id)
  if (error !== null) message.error(error)
}

async function newChat(): Promise<void> {
  cancelEdit()
  await store.newSession()
  await router.push({ name: 'chat' })
  emit('navigate')
}

function sessionTitle(item: ChatSessionSummary): string {
  return item.title || '新对话'
}

onMounted(() => {
  if (store.sessions.length === 0) void store.loadSessions()
})
onBeforeUnmount(() => { editVersion += 1 })
</script>

<template>
  <div
    class="app-sidebar-inner flex h-full min-h-0 flex-col gap-2 p-3"
    :class="{ 'is-drawer': props.variant === 'drawer' }"
  >
    <div class="flex shrink-0 items-center gap-2 px-1 py-1">
      <span class="app-sidebar-mark" aria-hidden="true">◆</span>
      <span class="app-brand text-base">热点搭子</span>
    </div>

    <n-button
      type="primary"
      block
      class="shrink-0"
      :disabled="store.creatingSession"
      :loading="store.creatingSession"
      @click="newChat"
    >
      ＋ 新会话
    </n-button>

    <nav class="flex shrink-0 flex-col gap-1" aria-label="主导航">
      <router-link
        v-for="item in navItems"
        :key="item.name"
        class="app-nav-link flex items-center gap-2 px-3 py-2"
        :class="{ 'is-active': activeNav === item.name }"
        :aria-current="activeNav === item.name ? 'page' : undefined"
        :to="{ name: item.name }"
        @click="emit('navigate')"
      >
        <span aria-hidden="true">{{ item.icon }}</span>
        <span class="nav-label">{{ item.label }}</span>
      </router-link>
    </nav>

    <div class="history-section flex min-h-0 flex-1 flex-col border-t border-line pt-2">
      <div class="shrink-0 px-1 pb-1 text-xs text-ink-muted">历史会话</div>
      <div class="min-h-0 flex-1 overflow-y-auto">
        <div v-if="store.sessionsLoading" class="px-1 text-sm text-ink-muted">加载中…</div>
        <div v-else-if="store.sessionsError" class="px-1 text-sm text-ink-muted">
          {{ store.sessionsError }}
        </div>
        <n-empty v-else-if="store.sessions.length === 0" size="small" description="还没有会话" />
        <div v-else class="flex flex-col gap-1">
          <div
            v-for="item in store.sessions"
            :key="item.session_id"
            class="chat-session-item"
            :class="{ 'is-active': item.session_id === store.currentId }"
          >
            <n-input
              v-if="editingId === item.session_id"
              v-model:value="editingTitle"
              class="session-rename-input"
              size="small"
              maxlength="100"
              aria-label="会话名称"
              :disabled="saving"
              @keydown="onEditKeydown"
              @blur="saveEdit"
            />
            <template v-else>
              <button
                type="button"
                class="session-main"
                :aria-current="item.session_id === store.currentId ? 'true' : undefined"
                :disabled="editingId !== null"
                @click="openItem(item)"
              >
                <span class="line-clamp-1 font-medium">{{ sessionTitle(item) }}</span>
                <span class="mt-0.5 line-clamp-1 block text-xs text-ink-muted">
                  {{ item.message_count }} 条 · {{ item.last_message_preview }}
                </span>
              </button>
              <n-tooltip>
                <template #trigger>
                  <button
                    type="button"
                    class="session-rename"
                    aria-label="重命名"
                    @click.stop="startEdit(item)"
                  >
                    ✎
                  </button>
                </template>
                重命名
              </n-tooltip>
              <n-tooltip>
                <template #trigger>
                  <button
                    type="button"
                    class="session-delete"
                    aria-label="删除对话"
                    :disabled="store.busy && item.session_id === store.currentId"
                    @click.stop="removeItem(item)"
                  >
                    ⌫
                  </button>
                </template>
                删除对话
              </n-tooltip>
            </template>
          </div>
        </div>
      </div>
    </div>

    <div class="shrink-0 border-t border-line pt-2">
      <button type="button" class="theme-toggle" @click="appStore.toggleTheme()">
        ◐ {{ appStore.isDark ? '浅色' : '暗色' }}
      </button>
    </div>
  </div>
</template>
