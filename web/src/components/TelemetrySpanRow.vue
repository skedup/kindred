<script setup lang="ts">
import { computed, ref } from 'vue'
import type { TelemetrySpanDetail } from '../api/types'
import {
  formatTelemetryCount,
  formatTelemetryDuration,
  formatTelemetryTime,
  relativeTelemetryWidth,
  telemetrySpanKindLabel,
} from '../lib/telemetry'

const props = defineProps<{
  span: TelemetrySpanDetail
  maximumDuration: number
  child?: boolean
}>()

const expanded = ref(false)
const model = computed(() => props.span.response_model ?? props.span.requested_model)
const usage = computed(() => props.span.usage)
const width = computed(() => relativeTelemetryWidth(props.span.duration_us, props.maximumDuration))

function formatCost(value: number): string {
  const dollars = value / 1_000_000
  return `$${dollars.toFixed(6).replace(/0+$/, '').replace(/\.$/, '')}`
}
</script>

<template>
  <article class="span-row" :class="[{ child }, span.status]">
    <button class="span-toggle" type="button" :aria-expanded="expanded" @click="expanded = !expanded">
      <span class="sequence">#{{ span.sequence }}</span>
      <span class="span-main">
        <span class="span-title">
          <strong>{{ span.name }}</strong>
          <small>{{ telemetrySpanKindLabel(span) }}</small>
          <small class="status">{{ span.status === 'succeeded' ? '成功' : span.status === 'failed' ? '失败' : '运行中' }}</small>
        </span>
        <span class="duration-track" aria-hidden="true">
          <i :style="{ width: `${width}%` }" />
        </span>
      </span>
      <span class="duration">{{ formatTelemetryDuration(span.duration_us) }}</span>
      <span class="chevron" aria-hidden="true">{{ expanded ? '−' : '+' }}</span>
    </button>

    <div v-if="expanded" class="span-detail">
      <dl>
        <template v-if="span.llm_role">
          <dt>Role</dt><dd>{{ span.llm_role }}</dd>
        </template>
        <template v-if="span.round_index != null">
          <dt>Round</dt><dd>{{ span.round_index }}</dd>
        </template>
        <template v-if="span.source_round_index != null">
          <dt>Source round</dt><dd>{{ span.source_round_index }}</dd>
        </template>
        <template v-if="span.tool_effect">
          <dt>Effect</dt><dd>{{ span.tool_effect }}</dd>
        </template>
        <template v-if="span.provider">
          <dt>Provider</dt><dd>{{ span.provider }}</dd>
        </template>
        <template v-if="model">
          <dt>Model</dt><dd>{{ model }}</dd>
        </template>
        <template v-if="span.http_status != null">
          <dt>HTTP</dt><dd>{{ span.http_status }}</dd>
        </template>
        <template v-if="span.error_type">
          <dt>Error</dt><dd>{{ span.error_type }}</dd>
        </template>
        <dt>Started</dt><dd>{{ formatTelemetryTime(span.started_at) }}</dd>
      </dl>

      <div v-if="usage" class="usage-grid">
        <span><small>Input</small><strong>{{ formatTelemetryCount(usage.input_tokens) }}</strong></span>
        <span><small>Output</small><strong>{{ formatTelemetryCount(usage.output_tokens) }}</strong></span>
        <span><small>Total</small><strong>{{ formatTelemetryCount(usage.total_tokens) }}</strong></span>
        <span><small>Usage</small><strong>{{ usage.reconciliation_status }}</strong></span>
        <span v-if="usage.cache_read_input_tokens != null">
          <small>Cache read</small><strong>{{ formatTelemetryCount(usage.cache_read_input_tokens) }}</strong>
        </span>
        <span v-if="usage.cache_write_input_tokens != null">
          <small>Cache write</small><strong>{{ formatTelemetryCount(usage.cache_write_input_tokens) }}</strong>
        </span>
        <span v-if="usage.reasoning_output_tokens != null">
          <small>Reasoning</small><strong>{{ formatTelemetryCount(usage.reasoning_output_tokens) }}</strong>
        </span>
        <span v-if="usage.tool_use_prompt_tokens != null">
          <small>Tool prompt</small><strong>{{ formatTelemetryCount(usage.tool_use_prompt_tokens) }}</strong>
        </span>
        <span v-if="usage.unattributed_tokens != null">
          <small>Unattributed</small><strong>{{ formatTelemetryCount(usage.unattributed_tokens) }}</strong>
        </span>
        <span v-if="usage.provider_cost_microusd != null">
          <small>Provider cost</small><strong>{{ formatCost(usage.provider_cost_microusd) }}</strong>
        </span>
        <span v-if="usage.payload_json_bytes != null">
          <small>Payload bytes</small><strong>{{ formatTelemetryCount(usage.payload_json_bytes) }}</strong>
        </span>
        <span v-if="usage.tool_schema_json_chars != null">
          <small>Tool schema chars</small><strong>{{ formatTelemetryCount(usage.tool_schema_json_chars) }}</strong>
        </span>
        <span v-if="usage.model_history_json_chars != null">
          <small>History chars</small><strong>{{ formatTelemetryCount(usage.model_history_json_chars) }}</strong>
        </span>
        <span v-if="usage.tool_result_json_chars != null">
          <small>Tool result chars</small><strong>{{ formatTelemetryCount(usage.tool_result_json_chars) }}</strong>
        </span>
      </div>
    </div>
  </article>
</template>

<style scoped>
.span-row {
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.018);
}
.span-row.child {
  margin-left: 26px;
  border-left: 2px solid rgba(201, 168, 106, 0.34);
}
.span-row.failed {
  border-color: rgba(178, 86, 104, 0.52);
}
.span-toggle {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr) 74px 22px;
  align-items: center;
  width: 100%;
  padding: 11px 12px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
}
.span-toggle:hover {
  background: rgba(255, 255, 255, 0.025);
}
.sequence,
.duration {
  color: var(--text-faint);
  font-size: 0.68rem;
  font-variant-numeric: tabular-nums;
}
.span-main {
  min-width: 0;
}
.span-title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
}
.span-title strong {
  overflow: hidden;
  color: var(--text);
  font-size: 0.78rem;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.span-title small {
  flex: none;
  color: var(--text-faint);
  font-size: 0.62rem;
  text-transform: uppercase;
}
.span-title .status {
  padding: 1px 5px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.05);
  text-transform: none;
}
.failed .span-title .status {
  color: var(--accent-soft);
}
.duration-track {
  display: block;
  height: 3px;
  margin-top: 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.04);
}
.duration-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--gold);
}
.failed .duration-track i {
  background: var(--accent-soft);
}
.duration {
  padding-left: 10px;
  color: var(--text-dim);
  text-align: right;
}
.chevron {
  color: var(--text-faint);
  font-size: 1rem;
  text-align: right;
}
.span-detail {
  padding: 2px 14px 14px 54px;
}
dl {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 3px 10px;
  margin: 0;
  font-size: 0.68rem;
}
dt {
  color: var(--text-faint);
}
dd {
  overflow-wrap: anywhere;
  margin: 0;
  color: var(--text-dim);
}
.usage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(94px, 1fr));
  gap: 7px;
  margin-top: 11px;
}
.usage-grid span {
  display: flex;
  min-width: 0;
  flex-direction: column;
  padding: 7px 9px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.025);
}
.usage-grid small {
  color: var(--text-faint);
  font-size: 0.61rem;
}
.usage-grid strong {
  overflow: hidden;
  color: var(--text-dim);
  font-size: 0.7rem;
  font-weight: 550;
  text-overflow: ellipsis;
}

@media (max-width: 620px) {
  .span-row.child {
    margin-left: 12px;
  }
  .span-toggle {
    grid-template-columns: 34px minmax(0, 1fr) 60px 18px;
    padding: 10px 9px;
  }
  .span-title small:not(.status) {
    display: none;
  }
  .span-detail {
    padding-left: 43px;
  }
}
</style>
