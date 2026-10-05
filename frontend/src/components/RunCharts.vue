<script setup lang="ts">
import { NEmpty } from 'naive-ui'
import { computed } from 'vue'

import type { components } from '@/api/schema'
import EChart from '@/components/EChart.vue'
import {
  buildCostLatencyOption,
  buildCoverageOption,
  buildScoreDistributionOption,
} from '@/charts/options'
import { useAppStore } from '@/stores/app'
import { CHART_DARK, CHART_LIGHT } from '@/theme/tokens'

type RunDetail = components['schemas']['RunDetail']

const props = defineProps<{ run: RunDetail }>()

const appStore = useAppStore()

/** 图表跟随主题：暗色下换一套坐标轴 / 网格线 / 柱色（S5.7）。 */
const palette = computed(() => (appStore.isDark ? CHART_DARK : CHART_LIGHT))

const hasHotspots = computed(() => props.run.hotspots.length > 0)
const hasCandidates = computed(() => props.run.hotspots.some((hotspot) => hotspot.candidates.length > 0))

const coverageOption = computed(() => buildCoverageOption(props.run, palette.value))
const scoreOption = computed(() => buildScoreDistributionOption(props.run, palette.value))
const costOption = computed(() => buildCostLatencyOption(props.run, palette.value))
</script>

<template>
  <div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
    <section v-if="hasHotspots">
      <h3 class="mb-1 text-sm font-semibold text-ink-strong">
        要素覆盖度
      </h3>
      <EChart :option="coverageOption" />
    </section>

    <section>
      <h3 class="mb-1 text-sm font-semibold text-ink-strong">
        候选素材得分分布
      </h3>
      <EChart v-if="hasCandidates" :option="scoreOption" />
      <n-empty v-else description="没有命中的素材，无得分可分布" />
    </section>

    <section class="lg:col-span-2">
      <h3 class="mb-1 text-sm font-semibold text-ink-strong">
        成本与耗时
      </h3>
      <EChart :option="costOption" />
    </section>
  </div>
</template>
