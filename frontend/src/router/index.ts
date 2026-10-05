import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import AnalysisView from '@/views/AnalysisView.vue'
import RunHistoryView from '@/views/RunHistoryView.vue'
import RunResultView from '@/views/RunResultView.vue'

// 路由表随页面落地逐步补充（结果详情 S5.4 / 运行历史 S5.6）
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'analysis',
    component: AnalysisView,
  },
  {
    // 历史列表（S5.6）：静态路径优先级高于 /runs/:runId，两者互不冲突
    path: '/runs',
    name: 'run-history',
    component: RunHistoryView,
  },
  {
    path: '/runs/:runId',
    name: 'run-result',
    component: RunResultView,
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
