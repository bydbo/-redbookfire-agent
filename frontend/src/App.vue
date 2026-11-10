<script setup lang="ts">
import {
  NButton, NConfigProvider, NDrawer, NDrawerContent, NGlobalStyle, NMessageProvider,
  darkTheme,
} from 'naive-ui'
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import AppSidebar from '@/components/AppSidebar.vue'
import { useAppStore } from '@/stores/app'
import { buildThemeOverrides } from '@/theme/naive'

const appStore = useAppStore()
const route = useRoute()

const overrides = computed(() => buildThemeOverrides(appStore.palette))
const showDrawer = ref(false)
// 桌面对话页消息区自己滚，外层不滚；非对话页由 .app-content 外滚；移动端保留外滚降级。
const chatActive = computed(() => route.name === 'chat')
</script>

<template>
  <n-config-provider :theme="appStore.isDark ? darkTheme : null" :theme-overrides="overrides">
    <n-global-style />
    <!-- 硬前提：ChatView / AppSidebar 的 useMessage() 依赖它；包在 config-provider 内侧、壳层之外 -->
    <n-message-provider>
      <!-- 高度链：html/body/#app 都是 height:100%，这里 h-dvh 锁视口，
           内容区 flex-1 min-h-0 拿到确定高度，由内容区自己滚 -->
      <div class="app-shell flex h-dvh bg-surface-muted">
        <aside class="app-sidebar hidden md:flex md:w-60 lg:w-64 xl:w-72">
          <AppSidebar />
        </aside>
        <div class="flex min-w-0 flex-1 flex-col p-2">
          <div class="app-mobilebar flex md:hidden">
            <n-button
              size="small"
              quaternary
              aria-label="打开导航"
              :aria-expanded="showDrawer"
              @click="showDrawer = true"
            >
              ☰
            </n-button>
            <span class="app-brand text-sm">{{ appStore.title }}</span>
          </div>
          <div
            class="app-content min-h-0 flex-1 overflow-y-auto rounded-xl border border-line bg-surface"
            :class="{ 'app-content--chat': chatActive }"
          >
            <router-view />
          </div>
        </div>
      </div>

      <n-drawer v-model:show="showDrawer" placement="left" :width="'min(320px, 85vw)'">
        <n-drawer-content closable>
          <AppSidebar variant="drawer" @navigate="showDrawer = false" />
        </n-drawer-content>
      </n-drawer>
    </n-message-provider>
  </n-config-provider>
</template>
