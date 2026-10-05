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

type RunDetail = components['schemas']['RunDetail']

const props = defineProps<{ run: RunDetail }>()

const hasHotspots = computed(() => props.run.hotspots.length > 0)
const hasCandidates = computed(() => props.run.hotspots.some((hotspot) => hotspot.candidates.length > 0))

const coverageOption = computed(() => buildCoverageOption(props.run))
const scoreOption = computed(() => buildScoreDistributionOption(props.run))
const costOption = computed(() => buildCostLatencyOption(props.run))
</script>

<template>
  <div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
    <section v-if="hasHotspots">
      <h3 class="mb-1 text-sm font-semibold text-slate-700">
        要素覆盖度
      </h3>
      <EChart :option="coverageOption" />
    </section>

    <section>
      <h3 class="mb-1 text-sm font-semibold text-slate-700">
        候选素材得分分布
      </h3>
      <EChart v-if="hasCandidates" :option="scoreOption" />
      <n-empty v-else description="没有命中的素材，无得分可分布" />
    </section>

    <section class="lg:col-span-2">
      <h3 class="mb-1 text-sm font-semibold text-slate-700">
        成本与耗时
      </h3>
      <EChart :option="costOption" />
    </section>
  </div>
</template>
