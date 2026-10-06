<script setup lang="ts">
import {
  NAlert, NButton, NCard, NCheckbox, NDrawer, NDrawerContent, NEmpty, NInput, NModal,
  NPagination, NPopconfirm, NProgress, NSelect, NSpin, NTag,
} from 'naive-ui'
import { computed, onMounted, ref } from 'vue'

import type { MaterialItem, MaterialSource, MaterialType } from '@/api/materials'
import { apiUrl } from '@/api/client'
import { useMaterialsStore } from '@/stores/materials'

const store = useMaterialsStore()

onMounted(() => {
  void store.load()
})

// ---------- 工具条 ----------

const searchText = ref('')
const typeOptions = [
  { label: '全部类型', value: '' },
  { label: '视频', value: 'video' },
  { label: '图片', value: 'image' },
]
const sourceOptions = [
  { label: '全部来源', value: '' },
  { label: '人工说明', value: 'sidecar' },
  { label: '模型打标', value: 'vision' },
  { label: '文件名', value: 'filename' },
  { label: '历史遗留', value: 'legacy' },
]
const typeValue = computed(() => store.filters.type ?? '')
const sourceValue = computed(() => store.filters.source ?? '')
const dirItems = computed(() => [
  { label: `全部（${store.total}）`, value: null },
  ...store.dirs.map((entry) => ({ label: `${entry.path}（${entry.count}）`, value: entry.path })),
])

async function onSearch(): Promise<void> {
  await store.applyFilters({ q: searchText.value })
}

async function onType(value: string | number | null): Promise<void> {
  await store.applyFilters({ type: typeof value === 'string' && value ? value as MaterialType : null })
}

async function onSource(value: string | number | null): Promise<void> {
  await store.applyFilters({
    source: typeof value === 'string' && value ? value as MaterialSource : null,
  })
}

async function onDir(value: string | null): Promise<void> {
  await store.applyFilters({ dir: value })
}

// ---------- 上传 ----------

const fileInput = ref<HTMLInputElement | null>(null)
const dragging = ref(false)

function pickFiles(): void {
  fileInput.value?.click()
}

function startUpload(files: FileList | File[] | null | undefined): void {
  const list = Array.from(files ?? [])
  if (list.length > 0) void store.uploadFiles(list)
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  startUpload(input.files)
  input.value = ''
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  startUpload(event.dataTransfer?.files)
}

// ---------- 缩略图 / 展示 ----------

function thumbnailUrl(item: MaterialItem): string | null {
  return item.keyframes.length > 0 ? apiUrl(`/materials/${item.id}/keyframes/0`) : null
}

const frameFailed = ref<Record<string, boolean>>({})

function sizeText(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

function durationText(seconds: number): string {
  if (!seconds) return ''
  const total = Math.round(seconds)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

function timeText(iso: string): string {
  return iso ? new Date(iso).toLocaleString() : ''
}

const SOURCE_LABELS: Record<MaterialSource, string> = {
  sidecar: '人工说明',
  vision: '模型打标',
  filename: '文件名',
  legacy: '历史遗留',
}

const TYPE_LABELS: Record<MaterialType, string> = { video: '视频', image: '图片' }

// ---------- 编辑弹窗 ----------

const editTitle = ref('')
const editDescription = ref('')
const newTag = ref('')

function openEdit(item: MaterialItem): void {
  store.openEdit(item)
  editTitle.value = item.title
  editDescription.value = item.description
  newTag.value = ''
}

function addTag(): void {
  const value = newTag.value.trim().replace(/^#/, '')
  if (value && !store.editTags.includes(value)) store.editTags = [...store.editTags, value]
  newTag.value = ''
}

async function saveEdit(): Promise<void> {
  await store.saveEdit({
    title: editTitle.value,
    description: editDescription.value,
    tags: store.editTags,
  })
}

// ---------- 回收站 ----------

const trashError = computed(() => store.actionError)
</script>

<template>
  <main class="px-4 py-8">
    <div class="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <header class="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">
            素材库
          </h1>
          <p class="mt-1 text-sm text-ink">
            磁盘目录是唯一来源，这里是索引副本：导入、浏览、改标签、删进回收站都能在这里做
            （{{ store.total }} 条）。
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <n-button size="small" :loading="store.scanning" @click="store.scan()">
            扫描
          </n-button>
          <n-button size="small" @click="store.openTrash()">
            回收站
          </n-button>
          <n-button size="small" type="primary" @click="pickFiles">
            上传素材
          </n-button>
        </div>
      </header>

      <n-alert v-if="store.failure" type="error" :title="store.failure" />
      <n-alert v-if="store.actionError" type="error" :title="store.actionError" />
      <n-alert v-if="store.taskNote" type="info" :title="store.taskNote" />

      <!-- 上传区：选择文件 / 拖拽，多文件逐个上传各自进度 -->
      <n-card>
        <div
          class="flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-5 text-center"
          :class="dragging ? 'border-brand bg-surface' : 'border-line bg-surface'"
          @click="pickFiles"
          @dragover.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="onDrop"
        >
          <p class="text-sm font-medium text-ink-strong">
            把素材拖进来，或点这里选择文件
          </p>
          <p class="text-xs text-ink-muted">
            视频 / 图片，单个文件默认不超过 2 GB（<code class="rounded bg-surface-muted px-1">[upload].max_size_gb</code>）；
            落点是 <code class="rounded bg-surface-muted px-1">素材根/YYYY-MM/</code>，同名会自动加序号
          </p>
          <input
            ref="fileInput"
            type="file"
            multiple
            accept="video/*,image/*"
            class="hidden"
            @change="onFileChange"
          >
        </div>

        <div v-if="store.uploads.length > 0" class="mt-4 flex flex-col gap-2">
          <div class="flex items-center justify-between">
            <span class="text-sm font-medium text-ink-strong">上传队列</span>
            <n-button size="tiny" quaternary @click="store.clearUploads()">
              清空列表
            </n-button>
          </div>
          <div
            v-for="entry in store.uploads"
            :key="entry.name"
            class="flex items-center gap-3 rounded-lg border border-line px-3 py-2"
          >
            <span class="min-w-40 flex-1 truncate text-sm text-ink-strong">{{ entry.name }}</span>
            <n-progress
              class="w-40"
              type="line"
              :percentage="entry.percent"
              :height="8"
              :status="entry.status === 'failed' ? 'error' : 'default'"
            />
            <n-tag
              size="small"
              :type="entry.status === 'done' ? 'success'
                : entry.status === 'failed' ? 'error' : 'info'"
            >
              {{ entry.status === 'uploading' ? '上传中'
                : entry.status === 'indexing' ? '索引中'
                : entry.status === 'done' ? '已入库' : '失败' }}
            </n-tag>
            <span class="max-w-56 truncate text-xs text-ink-muted">{{ entry.message }}</span>
          </div>
        </div>
      </n-card>

      <n-card>
        <div class="flex flex-col gap-3">
          <div class="flex flex-wrap items-center gap-2">
            <n-input
              v-model:value="searchText"
              class="w-56"
              clearable
              placeholder="搜索标题或标签"
              @keyup.enter="onSearch"
              @clear="onSearch"
            />
            <n-button size="small" @click="onSearch">
              搜索
            </n-button>
            <n-select
              class="w-32"
              size="small"
              :value="typeValue"
              :options="typeOptions"
              @update:value="onType"
            />
            <n-select
              class="w-36"
              size="small"
              :value="sourceValue"
              :options="sourceOptions"
              @update:value="onSource"
            />
            <n-button size="small" quaternary @click="store.clearFilters()">
              重置
            </n-button>
            <div class="ml-auto flex items-center gap-1">
              <n-button
                size="small"
                :type="store.viewMode === 'grid' ? 'primary' : 'default'"
                @click="store.setView('grid')"
              >
                网格
              </n-button>
              <n-button
                size="small"
                :type="store.viewMode === 'list' ? 'primary' : 'default'"
                @click="store.setView('list')"
              >
                列表
              </n-button>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <n-button
              v-for="entry in dirItems"
              :key="entry.value ?? '__all__'"
              size="tiny"
              :type="store.filters.dir === entry.value ? 'primary' : 'default'"
              @click="onDir(entry.value)"
            >
              {{ entry.label }}
            </n-button>
          </div>

          <div v-if="store.selection.length > 0" class="flex items-center gap-3">
            <span class="text-sm text-ink">已选 {{ store.selection.length }} 条</span>
            <n-popconfirm positive-text="确认" negative-text="取消" @positive-click="store.trashSelected()">
              <template #trigger>
                <n-button size="small" type="error" quaternary>
                  批量删除
                </n-button>
              </template>
              删除的素材会进入回收站（可从回收站恢复），确认删除？
            </n-popconfirm>
            <n-button size="small" quaternary @click="store.clearSelection()">
              取消选择
            </n-button>
          </div>
        </div>
      </n-card>

      <n-spin :show="store.loading">
        <n-empty
          v-if="store.items.length === 0 && !store.loading"
          description="这里还没有素材"
          class="py-10"
        >
          <template #extra>
            <p class="text-xs text-ink-muted">
              把文件拖进上面的上传区，或先在 <code class="rounded bg-surface-muted px-1">
                data/materials/</code> 放好素材再点「扫描」
            </p>
          </template>
        </n-empty>

        <!-- 网格视图 -->
        <div
          v-else-if="store.viewMode === 'grid'"
          class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
        >
          <n-card v-for="item in store.items" :key="item.id" size="small">
            <div class="flex flex-col gap-2">
              <div class="relative h-36 w-full overflow-hidden rounded-lg bg-surface-muted">
                <img
                  v-if="thumbnailUrl(item) && !frameFailed[item.id]"
                  :src="thumbnailUrl(item) ?? ''"
                  :alt="item.title || '素材缩略图'"
                  class="h-full w-full object-cover"
                  @error="frameFailed[item.id] = true"
                >
                <div v-else class="flex h-full w-full items-center justify-center text-xs text-ink-muted">
                  无预览
                </div>
                <n-tag class="absolute left-2 top-2" size="tiny" :type="item.type === 'video' ? 'info' : 'success'">
                  {{ TYPE_LABELS[item.type] }}
                </n-tag>
                <n-checkbox
                  class="absolute right-2 top-2 rounded bg-surface px-1"
                  :checked="store.selection.includes(item.id)"
                  @update:checked="store.toggleSelected(item.id)"
                />
              </div>

              <div class="truncate font-medium text-ink-strong" :title="item.title">
                {{ item.title || '（未命名）' }}
              </div>
              <div class="flex flex-wrap items-center gap-1">
                <n-tag v-for="tag in item.tags.slice(0, 4)" :key="tag" size="tiny">
                  {{ tag }}
                </n-tag>
                <span v-if="item.tags.length === 0" class="text-xs text-ink-muted">无标签</span>
              </div>
              <div class="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                <span>{{ item.dir || '根目录' }}</span>
                <span>{{ SOURCE_LABELS[item.source] }}</span>
                <span v-if="durationText(item.duration_s)">{{ durationText(item.duration_s) }}</span>
                <span>{{ sizeText(item.size_bytes) }}</span>
                <span>{{ timeText(item.indexed_at) }}</span>
              </div>
              <div class="flex items-center gap-2">
                <n-button size="tiny" @click="openEdit(item)">
                  编辑
                </n-button>
                <n-popconfirm positive-text="确认" negative-text="取消" @positive-click="store.trashOne(item.id)">
                  <template #trigger>
                    <n-button size="tiny" quaternary type="error">
                      删除
                    </n-button>
                  </template>
                  移入回收站（可恢复），确认删除「{{ item.title }}」？
                </n-popconfirm>
              </div>
            </div>
          </n-card>
        </div>

        <!-- 列表视图 -->
        <n-card v-else size="small">
          <div class="flex flex-col divide-y divide-line">
            <div
              v-for="item in store.items"
              :key="item.id"
              class="flex items-center gap-3 py-3 first:pt-0 last:pb-0"
            >
              <n-checkbox
                :checked="store.selection.includes(item.id)"
                @update:checked="store.toggleSelected(item.id)"
              />
              <div class="h-12 w-20 shrink-0 overflow-hidden rounded bg-surface-muted">
                <img
                  v-if="thumbnailUrl(item) && !frameFailed[item.id]"
                  :src="thumbnailUrl(item) ?? ''"
                  alt=""
                  class="h-full w-full object-cover"
                  @error="frameFailed[item.id] = true"
                >
              </div>
              <div class="min-w-0 flex-1">
                <div class="truncate font-medium text-ink-strong">
                  {{ item.title || '（未命名）' }}
                </div>
                <div class="mt-1 flex flex-wrap items-center gap-1 text-xs text-ink-muted">
                  <n-tag size="tiny" :type="item.type === 'video' ? 'info' : 'success'">
                    {{ TYPE_LABELS[item.type] }}
                  </n-tag>
                  <span>{{ item.dir || '根目录' }}</span>
                  <span>{{ SOURCE_LABELS[item.source] }}</span>
                  <span>{{ sizeText(item.size_bytes) }}</span>
                  <span>{{ timeText(item.indexed_at) }}</span>
                  <n-tag v-for="tag in item.tags.slice(0, 4)" :key="tag" size="tiny">
                    {{ tag }}
                  </n-tag>
                </div>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <n-button size="tiny" @click="openEdit(item)">
                  编辑
                </n-button>
                <n-popconfirm positive-text="确认" negative-text="取消" @positive-click="store.trashOne(item.id)">
                  <template #trigger>
                    <n-button size="tiny" quaternary type="error">
                      删除
                    </n-button>
                  </template>
                  移入回收站（可恢复），确认删除「{{ item.title }}」？
                </n-popconfirm>
              </div>
            </div>
          </div>
        </n-card>
      </n-spin>

      <div v-if="store.pageCount > 1" class="flex justify-center">
        <n-pagination
          :page="store.page"
          :page-count="store.pageCount"
          @update:page="store.goPage"
        />
      </div>
    </div>

    <!-- 编辑弹窗 -->
    <n-modal
      :show="store.editing !== null"
      :mask-closable="!store.editSaving"
      @update:show="(value) => { if (!value) store.closeEdit() }"
    >
      <n-card class="w-[32rem] max-w-full" title="编辑素材" :bordered="false">
        <div class="flex flex-col gap-4">
          <div>
            <div class="mb-1 text-sm font-medium text-ink-strong">
              标题
            </div>
            <n-input v-model:value="editTitle" maxlength="100" placeholder="素材标题" />
          </div>
          <div>
            <div class="mb-1 text-sm font-medium text-ink-strong">
              描述
            </div>
            <n-input
              v-model:value="editDescription"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 5 }"
              placeholder="一句话说明这条素材能怎么用"
            />
          </div>
          <div>
            <div class="mb-1 text-sm font-medium text-ink-strong">
              标签（最多 14 个）
            </div>
            <div class="flex flex-wrap items-center gap-1">
              <n-tag
                v-for="tag in store.editTags"
                :key="tag"
                closable
                size="small"
                @close="store.editTags = store.editTags.filter((item) => item !== tag)"
              >
                {{ tag }}
              </n-tag>
              <span v-if="store.editTags.length === 0" class="text-xs text-ink-muted">还没有标签</span>
            </div>
            <div class="mt-2 flex items-center gap-2">
              <n-input
                v-model:value="newTag"
                size="small"
                class="w-40"
                placeholder="加一个标签"
                @keyup.enter="addTag"
              />
              <n-button size="small" @click="addTag">
                添加
              </n-button>
            </div>
          </div>
          <p class="text-xs text-ink-muted">
            保存会同时写数据库和同名旁车文件（<code class="rounded bg-surface-muted px-1">
              {{ store.editing?.title }}.txt</code>），重扫描后仍然是这份人工说明。
          </p>
        </div>
        <template #footer>
          <div class="flex justify-end gap-2">
            <n-button size="small" :disabled="store.editSaving" @click="store.closeEdit()">
              取消
            </n-button>
            <n-button size="small" type="primary" :loading="store.editSaving" @click="saveEdit">
              保存
            </n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <!-- 回收站抽屉 -->
    <n-drawer
      :show="store.trashOpen"
      :width="520"
      placement="right"
      @update:show="(value) => { if (!value) store.closeTrash() }"
    >
      <n-drawer-content title="回收站" closable>
        <div class="flex flex-col gap-3">
          <div class="flex items-center justify-between">
            <span class="text-sm text-ink">共 {{ store.trash.length }} 条</span>
            <n-popconfirm positive-text="确认" negative-text="取消" @positive-click="store.purgeAll()">
              <template #trigger>
                <n-button size="small" type="error" quaternary :disabled="store.trash.length === 0">
                  清空回收站
                </n-button>
              </template>
              真删回收站里的全部文件，**不可恢复**，确认？
            </n-popconfirm>
          </div>
          <n-alert v-if="trashError" type="error" :title="trashError" />
          <n-spin :show="store.trashLoading">
            <n-empty v-if="store.trash.length === 0" description="回收站是空的" class="py-8" />
            <div v-else class="flex flex-col divide-y divide-line">
              <div
                v-for="entry in store.trash"
                :key="entry.path"
                class="flex items-center gap-2 py-3 first:pt-0"
              >
                <div class="min-w-0 flex-1">
                  <div class="truncate text-sm text-ink-strong">{{ entry.name }}</div>
                  <div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                    <span>{{ entry.path }}</span>
                    <span>{{ sizeText(entry.size_bytes) }}</span>
                    <span>{{ timeText(entry.mtime) }}</span>
                  </div>
                </div>
                <n-button size="tiny" @click="store.restoreOne(entry.path)">
                  恢复
                </n-button>
                <n-popconfirm positive-text="确认" negative-text="取消" @positive-click="store.purgePaths([entry.path])">
                  <template #trigger>
                    <n-button size="tiny" quaternary type="error">
                      真删
                    </n-button>
                  </template>
                  真删「{{ entry.name }}」，**不可恢复**，确认？
                </n-popconfirm>
              </div>
            </div>
          </n-spin>
        </div>
      </n-drawer-content>
    </n-drawer>
  </main>
</template>
