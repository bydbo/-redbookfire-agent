/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * 接口基地址，默认 /api（docs/contracts/配置契约.md §五）。
   * 开发环境由 Vite dev server 代理到后端，生产与 API 同源部署，无需改代码。
   */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
