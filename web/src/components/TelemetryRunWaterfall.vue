<script setup lang="ts">
import { computed } from 'vue'
import type { TelemetryRunDetailResponse, TelemetrySpanDetail } from '../api/types'
import {
  formatTelemetryCount,
  formatTelemetryDuration,
  formatTelemetryTime,
  telemetryRunKindLabel,
  telemetryStatusLabel,
} from '../lib/telemetry'
import TelemetrySpanRow from './TelemetrySpanRow.vue'

const props = defineProps<{ detail: TelemetryRunDetailResponse }>()
defineEmits<{ close: [] }>()

const allSpans = computed<TelemetrySpanDetail[]>(() =>
  props.detail.stages.flatMap((tree) => [tree.stage, ...tree.children]),
)
const maximumDuration = computed(() =>
  Math.max(0, ...allSpans.value.map((span) => span.duration_us ?? 0)),
)
</script>

<template>
  <section class="run-detail card" aria-label="运行详情">
    <header class="detail-head">
      <div>
        <div class="eyebrow">Run detail</div>
        <h2>
          {{ telemetryRunKindLabel(detail.run.run_kind) }}
          <span :class="['run-status', detail.run.status]">{{ telemetryStatusLabel(detail.run.status) }}</span>
        </h2>
        <p>{{ formatTelemetryTime(detail.run.started_at) }} · {{ formatTelemetryDuration(detail.run.duration_us) }}</p>
      </div>
      <button class="close" type="button" aria-label="关闭运行详情" @click="$emit('close')">关闭</button>
    </header>

    <div class="run-facts">
      <span><small>Token</small><strong>{{ formatTelemetryCount(detail.run.tokens.total_tokens) }}</strong></span>
      <span><small>Requests</small><strong>{{ detail.run.request_count }}</strong></span>
      <span><small>Coverage</small><strong>{{ detail.run.total_available }}/{{ detail.run.request_count }}</strong></span>
      <span><small>Spans</small><strong>{{ detail.run.span_count }}</strong></span>
      <span v-if="detail.run.tick_id != null"><small>Tick</small><strong>#{{ detail.run.tick_id }}</strong></span>
      <span v-if="detail.run.dream_date"><small>Dream</small><strong>{{ detail.run.dream_date }}</strong></span>
      <span v-if="detail.run.error_type"><small>Error</small><strong>{{ detail.run.error_type }}</strong></span>
    </div>

    <p class="waterfall-note">
      按全局 sequence 展示；横条只比较各 span 自身 duration，不表示精确的绝对时间对齐。点击一行可展开安全数值。
    </p>

    <div v-if="!detail.stages.length" class="empty">这个运行没有已记录的 stage。</div>
    <div v-else class="waterfall">
      <section v-for="tree in detail.stages" :key="tree.stage.span_id" class="stage-tree">
        <TelemetrySpanRow :span="tree.stage" :maximum-duration="maximumDuration" />
        <div v-if="tree.children.length" class="children">
          <TelemetrySpanRow
            v-for="span in tree.children"
            :key="span.span_id"
            :span="span"
            :maximum-duration="maximumDuration"
            child
          />
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.run-detail {
  margin-top: 18px;
}
.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.eyebrow {
  color: var(--text-faint);
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
h2 {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 2px 0 0;
  font-size: 1.04rem;
  font-weight: 600;
}
.detail-head p {
  margin: 3px 0 0;
  color: var(--text-faint);
  font-size: 0.72rem;
  font-variant-numeric: tabular-nums;
}
.close {
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}
.close:hover {
  border-color: var(--accent-soft);
  color: var(--text);
}
.run-status {
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--text-dim);
  font-size: 0.66rem;
  font-weight: 500;
}
.run-status.failed,
.run-status.stale {
  color: var(--accent-soft);
}
.run-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 18px 0;
}
.run-facts span {
  display: flex;
  min-width: 82px;
  flex-direction: column;
  padding: 7px 10px;
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.025);
}
.run-facts small {
  color: var(--text-faint);
  font-size: 0.61rem;
}
.run-facts strong {
  overflow: hidden;
  color: var(--text-dim);
  font-size: 0.74rem;
  font-weight: 550;
  text-overflow: ellipsis;
}
.waterfall-note {
  margin: 0 0 14px;
  color: var(--text-faint);
  font-size: 0.7rem;
}
.waterfall,
.stage-tree,
.children {
  display: grid;
  gap: 8px;
}
.waterfall {
  gap: 13px;
}
.empty {
  padding: 24px;
  color: var(--text-faint);
  text-align: center;
}

@media (max-width: 620px) {
  .run-detail {
    padding: 18px 14px;
  }
}
</style>
