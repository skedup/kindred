<script setup lang="ts">
// 一个 0-100 的细条 —— 给 engineer 指标视图用。叙事视图不铺这个。
import type { Gauge } from '../api/types'

defineProps<{ label: string; gauge: Gauge }>()
</script>

<template>
  <div class="gauge">
    <div class="gauge-head">
      <span class="gauge-label">{{ label }}</span>
      <span class="gauge-val">{{ gauge.value }}</span>
    </div>
    <div class="gauge-track">
      <div class="gauge-fill" :style="{ width: `${gauge.value}%` }" />
    </div>
    <div v-if="gauge.description" class="gauge-desc">{{ gauge.description }}</div>
  </div>
</template>

<style scoped>
.gauge {
  margin-bottom: 12px;
}
.gauge-head {
  display: flex;
  justify-content: space-between;
  font-size: 0.82rem;
  margin-bottom: 4px;
}
.gauge-label {
  color: var(--text-dim);
}
.gauge-val {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.gauge-track {
  height: 5px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
}
.gauge-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--accent-soft));
  border-radius: 3px;
  transition: width 0.4s ease;
}
.gauge-desc {
  font-size: 0.76rem;
  color: var(--text-faint);
  margin-top: 3px;
}
</style>
