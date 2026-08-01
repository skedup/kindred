<script setup lang="ts">
import { computed, ref } from 'vue'
import type { InteriorHistoryPoint } from '../api/types'
import {
  linePath,
  metricValue,
  nearestPointIndex,
  pointX,
  type InteriorSeries,
} from '../lib/interiorChart'

const props = defineProps<{
  title: string
  eyebrow: string
  points: InteriorHistoryPoint[]
  series: InteriorSeries[]
}>()

const plot = ref<HTMLElement | null>(null)
const activeIndex = ref<number | null>(null)
const plotWidth = 880
const plotHeight = 210

const activePoint = computed(() =>
  activeIndex.value == null ? null : (props.points[activeIndex.value] ?? null),
)
const cursorX = computed(() => {
  if (activeIndex.value == null) return 0
  return pointX(props.points, activeIndex.value) * plotWidth
})
const tooltipSide = computed(() => (cursorX.value / plotWidth > 0.64 ? 'left' : 'right'))

function updateFromPointer(event: PointerEvent): void {
  const rect = plot.value?.getBoundingClientRect()
  if (!rect || rect.width <= 0) return
  activeIndex.value = nearestPointIndex(props.points, (event.clientX - rect.left) / rect.width)
}

function moveCursor(offset: number): void {
  const current = activeIndex.value ?? props.points.length - 1
  activeIndex.value = Math.max(0, Math.min(props.points.length - 1, current + offset))
}

function formatTime(ts: string | null): string {
  if (!ts) return '时间未知'
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return ts
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
</script>

<template>
  <section class="trend-band">
    <header class="trend-band-head">
      <div>
        <div class="trend-eyebrow">{{ eyebrow }}</div>
        <h2>{{ title }}</h2>
      </div>
      <div class="trend-legend">
        <span v-for="item in series" :key="item.key" class="trend-legend-item">
          <i :style="{ backgroundColor: item.color }" />
          {{ item.label }}
          <b>{{ metricValue(points[points.length - 1]!, item) ?? '—' }}</b>
        </span>
      </div>
    </header>

    <div
      ref="plot"
      class="trend-plot"
      role="img"
      :aria-label="`${title}，共 ${points.length} 个 tick`"
      tabindex="0"
      @pointermove="updateFromPointer"
      @pointerdown="updateFromPointer"
      @pointerleave="activeIndex = null"
      @focus="activeIndex ??= points.length - 1"
      @keydown.left.prevent="moveCursor(-1)"
      @keydown.right.prevent="moveCursor(1)"
      @keydown.home.prevent="activeIndex = 0"
      @keydown.end.prevent="activeIndex = points.length - 1"
    >
      <div class="trend-y-axis" aria-hidden="true">
        <span>100</span><span>50</span><span>0</span>
      </div>
      <svg
        class="trend-svg"
        :viewBox="`0 0 ${plotWidth} ${plotHeight}`"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <line v-for="y in [0, 105, 210]" :key="y" x1="0" :y1="y" :x2="plotWidth" :y2="y" />
        <path
          v-for="item in series"
          :key="item.key"
          :d="linePath(points, item, plotWidth, plotHeight)"
          :stroke="item.color"
        />
        <g v-if="activePoint">
          <line class="trend-cursor" :x1="cursorX" y1="0" :x2="cursorX" :y2="plotHeight" />
          <circle
            v-for="item in series"
            :key="item.key"
            :cx="cursorX"
            :cy="
              plotHeight -
              ((metricValue(activePoint, item) ?? 0) / 100) * plotHeight
            "
            r="4"
            :fill="item.color"
            :class="{ hidden: metricValue(activePoint, item) == null }"
          />
        </g>
      </svg>

      <div class="trend-x-axis" aria-hidden="true">
        <span>#{{ points[0]?.id }}</span>
        <span>#{{ points[Math.floor((points.length - 1) / 2)]?.id }}</span>
        <span>#{{ points[points.length - 1]?.id }}</span>
      </div>

      <div
        v-if="activePoint"
        class="trend-tooltip"
        :class="tooltipSide"
        :style="{ left: `${(cursorX / plotWidth) * 100}%` }"
      >
        <div class="trend-tooltip-head">
          <strong>Tick #{{ activePoint.id }}</strong>
          <span>{{ formatTime(activePoint.ts) }}</span>
        </div>
        <div class="trend-tooltip-activity">
          {{ activePoint.activity_name || '无活动' }}
          <span v-if="activePoint.activity_step"> / {{ activePoint.activity_step }}</span>
        </div>
        <p v-if="activePoint.note">{{ activePoint.note }}</p>
        <div class="trend-tooltip-values">
          <span v-for="item in series" :key="item.key">
            <i :style="{ backgroundColor: item.color }" />
            {{ item.label }}
            <b>{{ metricValue(activePoint, item) ?? '—' }}</b>
          </span>
        </div>
        <div class="trend-tooltip-foot">
          <span>{{ activePoint.trigger_source || '未知来源' }}</span>
          <span v-if="activePoint.significance != null">
            significance {{ activePoint.significance }}
          </span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.trend-band {
  padding: 24px 0 30px;
  border-top: 1px solid var(--border);
}
.trend-band:first-child {
  border-top: 0;
  padding-top: 0;
}
.trend-band:last-child {
  padding-bottom: 0;
}
.trend-band-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 18px;
}
.trend-eyebrow {
  color: var(--text-faint);
  font-size: 0.72rem;
  text-transform: uppercase;
}
h2 {
  margin: 2px 0 0;
  font-size: 1rem;
  font-weight: 600;
}
.trend-legend {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px 14px;
  color: var(--text-dim);
  font-size: 0.74rem;
}
.trend-legend-item {
  white-space: nowrap;
}
.trend-legend i,
.trend-tooltip-values i {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 5px;
  border-radius: 50%;
}
.trend-legend b {
  color: var(--text);
  margin-left: 4px;
  font-variant-numeric: tabular-nums;
}
.trend-plot {
  position: relative;
  height: 260px;
  padding: 8px 0 28px 38px;
  outline: none;
  touch-action: pan-y;
}
.trend-plot:focus-visible {
  box-shadow: 0 0 0 1px var(--accent-soft);
}
.trend-svg {
  width: 100%;
  height: 210px;
  overflow: visible;
}
.trend-svg > line {
  stroke: rgba(232, 221, 217, 0.1);
  stroke-width: 1;
  vector-effect: non-scaling-stroke;
}
.trend-svg path {
  fill: none;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  vector-effect: non-scaling-stroke;
}
.trend-svg .trend-cursor {
  stroke: rgba(232, 221, 217, 0.55);
  stroke-width: 1;
  stroke-dasharray: 3 4;
  vector-effect: non-scaling-stroke;
}
.trend-svg circle {
  stroke: var(--bg-card);
  stroke-width: 2;
  vector-effect: non-scaling-stroke;
}
.trend-svg circle.hidden {
  display: none;
}
.trend-y-axis {
  position: absolute;
  left: 0;
  top: 2px;
  bottom: 36px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  color: var(--text-faint);
  font-size: 0.68rem;
  font-variant-numeric: tabular-nums;
}
.trend-x-axis {
  display: flex;
  justify-content: space-between;
  color: var(--text-faint);
  font-size: 0.68rem;
  font-variant-numeric: tabular-nums;
}
.trend-tooltip {
  position: absolute;
  z-index: 3;
  top: 18px;
  width: min(280px, calc(100% - 52px));
  padding: 12px 14px;
  border: 1px solid rgba(201, 168, 106, 0.36);
  border-radius: 6px;
  background: rgba(26, 20, 22, 0.96);
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.42);
  pointer-events: none;
}
.trend-tooltip.right {
  transform: translateX(12px);
}
.trend-tooltip.left {
  transform: translateX(calc(-100% - 12px));
}
.trend-tooltip-head,
.trend-tooltip-foot {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}
.trend-tooltip-head {
  font-size: 0.78rem;
}
.trend-tooltip-head span,
.trend-tooltip-foot {
  color: var(--text-faint);
}
.trend-tooltip-activity {
  margin-top: 4px;
  color: var(--gold);
  font-size: 0.76rem;
}
.trend-tooltip p {
  display: -webkit-box;
  overflow: hidden;
  margin: 8px 0;
  color: var(--text-dim);
  font-size: 0.76rem;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.trend-tooltip-values {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 12px;
  margin-top: 8px;
  font-size: 0.72rem;
}
.trend-tooltip-values span {
  display: flex;
  align-items: center;
  color: var(--text-dim);
}
.trend-tooltip-values b {
  margin-left: auto;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.trend-tooltip-foot {
  margin-top: 9px;
  padding-top: 7px;
  border-top: 1px solid var(--border);
  font-size: 0.66rem;
}

@media (max-width: 620px) {
  .trend-band-head {
    display: block;
  }
  .trend-legend {
    justify-content: flex-start;
    margin-top: 10px;
  }
  .trend-plot {
    height: 238px;
  }
  .trend-svg {
    height: 188px;
  }
  .trend-y-axis {
    bottom: 36px;
  }
}
</style>
