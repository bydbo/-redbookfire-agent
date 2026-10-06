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
    <!-- 高度链（对话页独立滚动的前提）：html/body/#app 都是 height:100%（main.css），
         这里用 h-dvh 把外壳锁在视口内，内容区 flex-1 + min-h-0 拿到确定高度，
         **由内容区自己滚**——所以文档级滚动条彻底消失，顶栏永远可见。
         对话页再在内部接管滚动（它的 <main> 用 h-full，外壳这层就没有可滚的内容）。 -->
    <div class="flex h-dvh flex-col">
      <!-- 四页共用的毛玻璃顶栏（S5.7）：品牌 + 导航 + 暗色开关。
           窄屏（375px）下导航横向滚动，品牌让位——见 S7.5 的真机截图验收。 -->
      <header class="app-topbar">
        <div class="mx-auto flex w-full max-w-5xl items-center gap-3 px-4 py-3">
          <span class="app-brand shrink-0 text-base max-sm:hidden">{{ appStore.title }}</span>
          <nav class="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto" aria-label="主导航">
            <router-link
              class="app-nav-link shrink-0"
              :class="{ 'is-active': activeNav === 'chat' }"
              :to="{ name: 'chat' }"
            >
              聊天
            </router-link>
            <router-link
              class="app-nav-link shrink-0"
              :class="{ 'is-active': activeNav === 'analysis' }"
              :to="{ name: 'analysis' }"
            >
              分析台
            </router-link>
            <router-link
              class="app-nav-link shrink-0"
              :class="{ 'is-active': activeNav === 'materials' }"
              :to="{ name: 'materials' }"
            >
              素材库
            </router-link>
            <router-link
              class="app-nav-link shrink-0"
              :class="{ 'is-active': activeNav === 'run-history' }"
              :to="{ name: 'run-history' }"
            >
              运行历史
            </router-link>
          </nav>
          <n-button class="shrink-0" size="small" @click="appStore.toggleTheme()">
            {{ appStore.isDark ? '浅色' : '暗色' }}
          </n-button>
        </div>
      </header>
      <div class="app-content min-h-0 flex-1 overflow-y-auto">
        <router-view />
      </div>
    </div>
  </n-config-provider>
</template>
