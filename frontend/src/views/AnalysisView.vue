<script setup lang="ts">
import { NAlert, NButton, NCard, NDynamicInput, NInput, NInputNumber, NProgress, NTag } from 'naive-ui'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

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
          输入一个或多个热点，在你的素材库里检索可蹭的素材并产出文案初稿（1–10 个热点，逐个分析）。
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
                <n-input
                  v-model:value="value.text"
                  type="textarea"
                  :autosize="{ minRows: 1, maxRows: 3 }"
                  maxlength="500"
                  placeholder="例如：某明星打羽毛球被拍，反差感拉满"
                />
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
    </div>
  </main>
</template>
