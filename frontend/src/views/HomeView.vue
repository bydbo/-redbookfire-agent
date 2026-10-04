<script setup lang="ts">
import { NButton, NCard, NTag } from 'naive-ui'

import { useAppStore } from '@/stores/app'

// S5.3 起本页替换为分析台；当前页面用于验证脚手架各链路（Router / Pinia / Naive UI / Tailwind / 环境变量）
const appStore = useAppStore()

// 接口基地址：配置契约 §五约定读取 VITE_API_BASE_URL，默认 /api
const apiBase = import.meta.env.VITE_API_BASE_URL ?? '/api'
</script>

<template>
  <main class="flex min-h-screen items-center justify-center bg-slate-50 p-6">
    <n-card class="w-full max-w-lg" :title="appStore.title">
      <div class="flex flex-col gap-4">
        <p class="text-sm text-slate-600">热点 → 素材匹配分析台（E5 脚手架验证页）</p>
        <div class="flex items-center gap-2">
          <n-tag type="success" size="small">
            Vue 3 + TS
          </n-tag>
          <n-tag type="info" size="small">
            Router + Pinia
          </n-tag>
          <n-tag type="warning" size="small">
            Naive UI + Tailwind
          </n-tag>
        </div>
        <p class="text-xs text-slate-500">
          接口基地址：<code class="rounded bg-slate-100 px-1 py-0.5">{{ apiBase }}</code>
        </p>
        <div>
          <n-button type="primary">
            脚手架已就绪
          </n-button>
        </div>
      </div>
    </n-card>
  </main>
</template>
