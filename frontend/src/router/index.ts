import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import HomeView from '@/views/HomeView.vue'

// 路由表随页面落地逐步补充（分析台 S5.3 / 结果详情 S5.4 / 运行历史 S5.6）
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home',
    component: HomeView,
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
