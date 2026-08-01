<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchInteriorHistory } from '../api/client'
import type { InteriorHistoryPoint } from '../api/types'
import type { InteriorSeries } from '../lib/interiorChart'
import InteriorLineChart from './InteriorLineChart.vue'

const ranges = [60, 120, 240] as const
const limit = ref<(typeof ranges)[number]>(120)
const points = ref<InteriorHistoryPoint[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

const gaugeSeries: InteriorSeries[] = [
  { key: 'body', label: '身体', color: '#4fb7a8', group: 'gauges' },
  { key: 'mood', label: '心情', color: '#d88a9c', group: 'gauges' },
  { key: 'inner_pulse', label: '内在脉动', color: '#c9a86a', group: 'gauges' },
]
const needsSeries: InteriorSeries[] = [
  { key: 'hunger', label: '饥饿', color: '#e08b63', group: 'needs' },
  { key: 'energy', label: '精力', color: '#e1c85a', group: 'needs' },
  { key: 'fatigue', label: '疲劳', color: '#8d83c7', group: 'needs' },
  { key: 'comfort', label: '舒适', color: '#66b8b0', group: 'needs' },
  { key: 'social', label: '社交', color: '#d2709b', group: 'needs' },
  { key: 'stimulation', label: '刺激', color: '#5e9ed6', group: 'needs' },
  { key: 'aesthetic', label: '审美', color: '#ba8f5c', group: 'needs' },
]
const affectSeries: InteriorSeries[] = [
  { key: 'stress', label: '压力', color: '#d55f68', group: 'affect' },
  { key: 'focus', label: '专注', color: '#6aa6d9', group: 'affect' },
  { key: 'arousal', label: '唤醒', color: '#d47f46', group: 'affect' },
  { key: 'clarity', label: '清晰', color: '#78b878', group: 'affect' },
]

const rangeLabel = computed(() => {
  if (!points.value.length) return ''
  return `Tick #${points.value[0]?.id} — #${points.value[points.value.length - 1]?.id}`
})

async function load(nextLimit = limit.value): Promise<void> {
  limit.value = nextLimit
  loading.value = true
  error.value = null
  try {
    const response = await fetchInteriorHistory(nextLimit)
    points.value = response.points
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    loading.value = false
  }
}

onMounted(() => load())
</script>

<template>
  <div v-if="loading" class="state-msg">正在展开 ta 的内在轨迹……</div>
  <div v-else-if="error" class="state-msg error">读不到：{{ error }}</div>
  <div v-else-if="!points.length" class="state-msg">还没有足够的 tick 画出曲线。</div>

  <div v-else class="card interior-history">
    <header class="history-head">
      <div>
        <h1>内在曲线</h1>
        <p>{{ rangeLabel }} · {{ points.length }} 个 tick</p>
      </div>
      <div class="range-control" aria-label="趋势范围">
        <button
          v-for="value in ranges"
          :key="value"
          :class="{ active: limit === value }"
          @click="load(value)"
        >
          {{ value }}
        </button>
      </div>
    </header>

    <InteriorLineChart
      eyebrow="Gauge"
      title="身体与心情"
      :points="points"
      :series="gaugeSeries"
    />
    <InteriorLineChart
      eyebrow="Needs"
      title="需要"
      :points="points"
      :series="needsSeries"
    />
    <InteriorLineChart
      eyebrow="Affect"
      title="情绪动力"
      :points="points"
      :series="affectSeries"
    />
  </div>
</template>

<style scoped>
.interior-history {
  padding: 22px 24px 28px;
}
.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 24px;
}
.history-head h1 {
  margin: 0;
  font-size: 1.12rem;
  font-weight: 600;
}
.history-head p {
  margin: 2px 0 0;
  color: var(--text-faint);
  font-size: 0.76rem;
  font-variant-numeric: tabular-nums;
}
.range-control {
  display: inline-flex;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.range-control button {
  min-width: 44px;
  height: 30px;
  padding: 0 10px;
  border: 0;
  border-left: 1px solid var(--border);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 0.74rem;
  font-variant-numeric: tabular-nums;
}
.range-control button:first-child {
  border-left: 0;
}
.range-control button:hover {
  color: var(--text);
}
.range-control button.active {
  background: var(--accent);
  color: white;
}

@media (max-width: 620px) {
  .interior-history {
    padding: 18px 16px 22px;
  }
  .history-head {
    align-items: flex-start;
  }
  .range-control button {
    min-width: 38px;
    padding: 0 7px;
  }
}
</style>
