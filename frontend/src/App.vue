<script setup lang="ts">
import { NButton, NConfigProvider, NGlobalStyle, darkTheme } from 'naive-ui'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useAppStore } from '@/stores/app'
import { buildThemeOverrides } from '@/theme/naive'

const appStore = useAppStore()
const route = useRoute()

const overrides = computed(() => buildThemeOverrides(appStore.palette))
/** 结果详情也归属「运行历史」这一栏（同一条浏览路径）。 */
const activeNav = computed(() => (route.name === 'run-result' ? 'run-history' : route.name))
</script>

<template>
  <n-config-provider :theme="appStore.isDark ? darkTheme : null" :theme-overrides="overrides">
    <n-global-style />
    <div class="flex min-h-screen flex-col">
      <!-- 三页共用的毛玻璃顶栏（S5.7）：品牌 + 导航 + 暗色开关 -->
      <header class="app-topbar">
        <div class="mx-auto flex w-full max-w-5xl items-center gap-3 px-4 py-3">
          <span class="app-brand text-base">{{ appStore.title }}</span>
          <nav class="flex items-center gap-1">
            <router-link
              class="app-nav-link"
              :class="{ 'is-active': activeNav === 'analysis' }"
              :to="{ name: 'analysis' }"
            >
              分析台
            </router-link>
            <router-link
              class="app-nav-link"
              :class="{ 'is-active': activeNav === 'materials' }"
              :to="{ name: 'materials' }"
            >
              素材库
            </router-link>
            <router-link
              class="app-nav-link"
              :class="{ 'is-active': activeNav === 'run-history' }"
              :to="{ name: 'run-history' }"
            >
              运行历史
            </router-link>
          </nav>
          <n-button class="ml-auto shrink-0" size="small" @click="appStore.toggleTheme()">
            {{ appStore.isDark ? '浅色' : '暗色' }}
          </n-button>
        </div>
      </header>
      <router-view />
    </div>
  </n-config-provider>
</template>
