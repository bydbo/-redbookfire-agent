import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import { DARK, LIGHT, type Palette } from '@/theme/tokens'

/** 应用级状态：标题 + 主题（S5.7）。 */
export type ThemeMode = 'light' | 'dark'

/** 手动切换后的持久化键；没有这个键时跟随系统 `prefers-color-scheme`。 */
const STORAGE_KEY = 'xhs-theme'

function initialMode(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (stored === 'light' || stored === 'dark') return stored
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    // 隐私模式等取不到 localStorage 时退回浅色
    return 'light'
  }
}

export const useAppStore = defineStore('app', () => {
  const title = ref('小红书热点搭子')
  const mode = ref<ThemeMode>(initialMode())

  const isDark = computed(() => mode.value === 'dark')
  /** 供 Naive themeOverrides 与图表调色板使用（浅/深各一套字面值）。 */
  const palette = computed<Palette>(() => (isDark.value ? DARK : LIGHT))

  function apply(): void {
    document.documentElement.dataset.theme = mode.value
  }

  apply()
  watch(mode, () => {
    apply()
    try {
      window.localStorage.setItem(STORAGE_KEY, mode.value)
    } catch {
      // 存不进去也不影响本次会话
    }
  })

  function toggleTheme(): void {
    mode.value = isDark.value ? 'light' : 'dark'
  }

  return { title, mode, isDark, palette, toggleTheme }
})
