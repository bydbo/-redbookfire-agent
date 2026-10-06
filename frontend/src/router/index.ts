import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import AnalysisView from '@/views/AnalysisView.vue'
import ChatView from '@/views/ChatView.vue'

// 路由表随页面落地逐步补充（结果详情 S5.4 / 运行历史 S5.6 / 素材库 S6.5 / 对话首页 S7.5）。
// 性能（S5.7）：对话首页与分析台是首屏路径、保持同步加载；结果页与历史页懒加载，
// 这样 ECharts 只随结果页 chunk 走，首屏不再背它那 ~350 KiB；素材库同样是懒加载。
//
// 首页调整（S7.5）：`/` 改成对话，分析台挪到 `/analyze`——**路由名仍是 `analysis`**，
// 既有按 name 跳转的代码与用例都不用改。
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'chat',
    component: ChatView,
  },
  {
    path: '/analyze',
    name: 'analysis',
    component: AnalysisView,
  },
  {
    // 素材库（S6.5）：浏览 / 导入 / 整理，静态路径与 /runs 一样不会跟其它路由打架
    path: '/materials',
    name: 'materials',
    component: () => import('@/views/MaterialsView.vue'),
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
