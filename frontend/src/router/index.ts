import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import AnalysisView from '@/views/AnalysisView.vue'

// 路由表随页面落地逐步补充（结果详情 S5.4 / 运行历史 S5.6）。
// 性能（S5.7）：分析台是首屏、保持同步加载；结果页与历史页懒加载，
// 这样 ECharts 只随结果页 chunk 走，首屏不再背它那 ~350 KiB。
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
    component: () => import('@/views/RunHistoryView.vue'),
  },
  {
    path: '/runs/:runId',
    name: 'run-result',
    component: () => import('@/views/RunResultView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
