import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 应用级状态。脚手架阶段的最小 store 用于验证 Pinia 链路，业务 store 随页面落地补充。 */
export const useAppStore = defineStore('app', () => {
  const title = ref('小红书热点搭子')

  return { title }
})
