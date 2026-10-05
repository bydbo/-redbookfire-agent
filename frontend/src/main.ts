import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'

import '@/assets/main.css'
// 主题层必须在 Tailwind 之后引入：语义变量与毛玻璃层要能覆盖工具类（S5.7）
import '@/assets/theme.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)

app.mount('#app')
