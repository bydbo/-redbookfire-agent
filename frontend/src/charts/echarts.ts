import { BarChart } from 'echarts/charts'
import {
  AriaComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

// ECharts 按需注册（S5.5）：只引本页用到的柱状图与配套组件，
// 避免整包引入撑大产物（S5.7 的 Lighthouse 性能分目标 ≥ 80）。
// 选型理由见 ADR 0008 与 docs/技术栈.md 的「前端图表」一行。
echarts.use([
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
  AriaComponent,
  CanvasRenderer,
])

export { echarts }
