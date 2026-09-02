<script setup lang="ts">
import { computed } from 'vue'
import { relativeTelemetryWidth } from '../lib/telemetry'

export interface TelemetryBarItem {
  key: string
  label: string
  value: number
  valueLabel: string
  secondaryValue?: number
  secondaryLabel?: string
}

const props = defineProps<{
  title: string
  eyebrow: string
  items: TelemetryBarItem[]
  emptyMessage: string
  primaryLegend: string
  secondaryLegend?: string
}>()

const maximum = computed(() =>
  Math.max(
    0,
    ...props.items.map((item) => Math.max(item.value, item.secondaryValue ?? 0)),
  ),
)
</script>

<template>
  <section class="telemetry-chart">
    <header>
      <div>
        <div class="eyebrow">{{ eyebrow }}</div>
        <h3>{{ title }}</h3>
      </div>
      <div class="legend">
        <span><i class="primary" />{{ primaryLegend }}</span>
        <span v-if="secondaryLegend"><i class="secondary" />{{ secondaryLegend }}</span>
      </div>
    </header>

    <p v-if="!items.length" class="empty">{{ emptyMessage }}</p>
    <div v-else class="bars" role="list" :aria-label="title">
      <div v-for="item in items" :key="item.key" class="bar-row" role="listitem">
        <div class="bar-label" :title="item.label">{{ item.label }}</div>
        <div class="bar-track" aria-hidden="true">
          <i
            v-if="item.secondaryValue != null"
            class="bar secondary"
            :style="{ width: `${relativeTelemetryWidth(item.secondaryValue, maximum)}%` }"
          />
          <i
            class="bar primary"
            :style="{ width: `${relativeTelemetryWidth(item.value, maximum)}%` }"
          />
        </div>
        <div class="bar-value">
          <strong>{{ item.valueLabel }}</strong>
          <span v-if="item.secondaryLabel">{{ item.secondaryLabel }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.telemetry-chart {
  min-width: 0;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.018);
}
header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.eyebrow {
  color: var(--text-faint);
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
h3 {
  margin: 2px 0 0;
  font-size: 0.94rem;
  font-weight: 600;
}
.legend {
  display: flex;
  gap: 9px;
  color: var(--text-faint);
  font-size: 0.68rem;
}
.legend span {
  white-space: nowrap;
}
.legend i {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 4px;
  border-radius: 2px;
}
.legend i.primary,
.bar.primary {
  background: var(--gold);
}
.legend i.secondary,
.bar.secondary {
  background: rgba(178, 86, 104, 0.48);
}
.bars {
  display: grid;
  gap: 11px;
}
.bar-row {
  display: grid;
  grid-template-columns: minmax(76px, 0.8fr) minmax(100px, 1.7fr) minmax(72px, auto);
  align-items: center;
  gap: 10px;
}
.bar-label {
  overflow: hidden;
  color: var(--text-dim);
  font-size: 0.72rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-track {
  position: relative;
  height: 7px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.045);
}
.bar {
  position: absolute;
  inset: 0 auto 0 0;
  border-radius: 999px;
}
.bar.primary {
  height: 3px;
  margin: 2px 0;
}
.bar-value {
  display: flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 5px;
  font-size: 0.68rem;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.bar-value strong {
  color: var(--text);
  font-weight: 600;
}
.bar-value span {
  color: var(--text-faint);
}
.empty {
  margin: 24px 0 12px;
  color: var(--text-faint);
  font-size: 0.76rem;
  text-align: center;
}

@media (max-width: 620px) {
  .telemetry-chart {
    padding: 15px;
  }
  .bar-row {
    grid-template-columns: minmax(66px, 0.7fr) minmax(80px, 1.3fr);
  }
  .bar-value {
    grid-column: 2;
    justify-content: flex-start;
    margin-top: -6px;
  }
}
</style>
