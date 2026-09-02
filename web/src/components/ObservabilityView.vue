<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  fetchTelemetryRunDetail,
  fetchTelemetryRuns,
  fetchTelemetrySummary,
} from '../api/client'
import type {
  TelemetryRunDetailResponse,
  TelemetryRunItem,
  TelemetryRunKind,
  TelemetryRunStatus,
  TelemetrySummaryResponse,
  TelemetryWindow,
} from '../api/types'
import {
  formatTelemetryCount,
  formatTelemetryDuration,
  formatTelemetryRatio,
  formatTelemetryTime,
  telemetryRunKindLabel,
  telemetryStatusLabel,
  telemetryWarnings,
} from '../lib/telemetry'
import TelemetryBarChart from './TelemetryBarChart.vue'
import TelemetryRunWaterfall from './TelemetryRunWaterfall.vue'

const windows: TelemetryWindow[] = ['24h', '7d', '30d']
const windowLabels: Record<TelemetryWindow, string> = { '24h': '24 小时', '7d': '7 天', '30d': '30 天' }
const dimensions = ['role', 'provider', 'model'] as const
type BreakdownDimension = (typeof dimensions)[number]

const selectedWindow = ref<TelemetryWindow>('24h')
const breakdownDimension = ref<BreakdownDimension>('role')
const summary = ref<TelemetrySummaryResponse | null>(null)
const baseline = ref<TelemetrySummaryResponse | null>(null)
const summaryLoading = ref(true)
const summaryError = ref(false)
let summaryRequest = 0
let baselineRequest = 0

const runKind = ref<TelemetryRunKind | ''>('')
const runStatus = ref<TelemetryRunStatus | ''>('')
const runs = ref<TelemetryRunItem[]>([])
const nextCursor = ref<string | null>(null)
const runsLoading = ref(true)
const runsError = ref(false)
let runsRequest = 0

const selectedRunId = ref<string | null>(null)
const detail = ref<TelemetryRunDetailResponse | null>(null)
const detailLoading = ref(false)
const detailError = ref(false)
let detailRequest = 0

const warnings = computed(() => (summary.value ? telemetryWarnings(summary.value) : []))
const failureRatio = computed(() => {
  if (!summary.value?.run_count) return null
  return summary.value.failed_run_count / summary.value.run_count
})
const breakdownItems = computed(() => {
  const current = summary.value
  if (!current) return []
  return current.breakdowns
    .filter((item) => item.dimension === breakdownDimension.value)
    .slice(0, 12)
    .map((item) => ({
      key: `${item.dimension}:${item.key}`,
      label: item.key,
      value: item.tokens.input_tokens,
      secondaryValue: item.tokens.total_tokens,
      valueLabel: formatTelemetryCount(item.tokens.input_tokens),
      secondaryLabel: `+${formatTelemetryCount(item.tokens.output_tokens)} / ${formatTelemetryCount(item.tokens.total_tokens)}`,
    }))
})
const breakdownItemCount = computed(
  () =>
    summary.value?.breakdowns.filter((item) => item.dimension === breakdownDimension.value).length ??
    0,
)
const stageLatencyItems = computed(() =>
  (summary.value?.stage_latencies ?? []).map((item) => ({
    key: item.name,
    label: item.name,
    value: item.latency.p50_us ?? 0,
    secondaryValue: item.latency.p95_us ?? 0,
    valueLabel: formatTelemetryDuration(item.latency.p50_us),
    secondaryLabel: formatTelemetryDuration(item.latency.p95_us),
  })),
)
const roundItems = computed(() =>
  (summary.value?.act.round_distribution ?? []).map((item) => ({
    key: String(item.rounds),
    label: `${item.rounds} round${item.rounds === 1 ? '' : 's'}`,
    value: item.run_count,
    valueLabel: `${item.run_count} run`,
  })),
)

async function loadSummary(window = selectedWindow.value): Promise<void> {
  const request = ++summaryRequest
  summaryLoading.value = true
  summaryError.value = false
  try {
    const response = await fetchTelemetrySummary(window)
    if (request === summaryRequest) summary.value = response
  } catch {
    if (request === summaryRequest) summaryError.value = true
  } finally {
    if (request === summaryRequest) summaryLoading.value = false
  }
}

async function changeWindow(window: TelemetryWindow): Promise<void> {
  if (window === selectedWindow.value && summary.value) return
  selectedWindow.value = window
  summary.value = null
  await loadSummary(window)
}

async function loadBaseline(): Promise<void> {
  const request = ++baselineRequest
  try {
    const response = await fetchTelemetrySummary('7d')
    if (request === baselineRequest) baseline.value = response
  } catch {
    if (request === baselineRequest) baseline.value = null
  }
}

async function loadRuns(reset = false): Promise<void> {
  const request = ++runsRequest
  runsLoading.value = true
  runsError.value = false
  if (reset) {
    runs.value = []
    nextCursor.value = null
  }
  try {
    const response = await fetchTelemetryRuns({
      cursor: reset ? undefined : (nextCursor.value ?? undefined),
      limit: 20,
      kind: runKind.value || undefined,
      status: runStatus.value || undefined,
    })
    if (request !== runsRequest) return
    const incoming = reset ? response.items : [...runs.value, ...response.items]
    runs.value = [...new Map(incoming.map((item) => [item.run_id, item])).values()]
    nextCursor.value = response.next_cursor
  } catch {
    if (request === runsRequest) runsError.value = true
  } finally {
    if (request === runsRequest) runsLoading.value = false
  }
}

async function resetRuns(): Promise<void> {
  closeDetail()
  await loadRuns(true)
}

async function refresh(): Promise<void> {
  closeDetail()
  await Promise.all([loadSummary(), loadBaseline(), loadRuns(true)])
}

async function openDetail(item: TelemetryRunItem): Promise<void> {
  const request = ++detailRequest
  selectedRunId.value = item.run_id
  detail.value = null
  detailLoading.value = true
  detailError.value = false
  try {
    const response = await fetchTelemetryRunDetail(item.run_id)
    if (request === detailRequest) detail.value = response
  } catch {
    if (request === detailRequest) detailError.value = true
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}

function closeDetail(): void {
  detailRequest += 1
  selectedRunId.value = null
  detail.value = null
  detailLoading.value = false
  detailError.value = false
}

function retryDetail(): void {
  const item = runs.value.find((run) => run.run_id === selectedRunId.value)
  if (item) void openDetail(item)
}

onMounted(() => {
  void Promise.all([loadSummary(), loadBaseline(), loadRuns(true)])
})
</script>

<template>
  <div class="observability-view">
    <header class="page-head">
      <div>
        <div class="eyebrow">Local telemetry</div>
        <h1>消耗与延迟</h1>
        <p>只展示本机安全数值，不包含 prompt、response 或 tool 内容。</p>
      </div>
      <button class="refresh" type="button" :disabled="summaryLoading || runsLoading" @click="refresh">
        刷新
      </button>
    </header>

    <div class="window-control" aria-label="汇总时间窗">
      <button
        v-for="window in windows"
        :key="window"
        :class="{ active: selectedWindow === window }"
        :aria-pressed="selectedWindow === window"
        type="button"
        @click="changeWindow(window)"
      >
        {{ windowLabels[window] }}
      </button>
    </div>

    <div v-if="summaryLoading && !summary" class="state-msg">正在汇总本地运行数据……</div>
    <div v-else-if="summaryError" class="state-msg error">
      汇总暂时无法读取。<button type="button" @click="loadSummary()">重试</button>
    </div>
    <div v-else-if="summary?.empty" class="state-msg">当前时间窗还没有 real tick 或 dream。</div>

    <template v-else-if="summary">
      <section v-if="warnings.length" class="warning-stack" aria-label="数据提示">
        <p v-for="warning in warnings" :key="warning">{{ warning }}</p>
      </section>

      <section class="summary-grid" aria-label="消耗汇总">
        <article class="metric-card">
          <small>Total token</small>
          <strong>{{ formatTelemetryCount(summary.tokens.total_tokens) }}</strong>
          <span>{{ formatTelemetryCount(summary.tokens.input_tokens) }} in · {{ formatTelemetryCount(summary.tokens.output_tokens) }} out</span>
        </article>
        <article class="metric-card">
          <small>Run p95</small>
          <strong>{{ formatTelemetryDuration(summary.run_latency.p95_us) }}</strong>
          <span>p50 {{ formatTelemetryDuration(summary.run_latency.p50_us) }} · 7d p95 {{ formatTelemetryDuration(baseline?.run_latency.p95_us) }}</span>
        </article>
        <article class="metric-card">
          <small>Usage coverage</small>
          <strong>{{ formatTelemetryRatio(summary.coverage.total_ratio) }}</strong>
          <span>{{ summary.coverage.total_available }}/{{ summary.coverage.request_count }} total · {{ formatTelemetryRatio(summary.coverage.breakdown_ratio) }} breakdown</span>
        </article>
        <article class="metric-card">
          <small>Failures</small>
          <strong>{{ formatTelemetryRatio(failureRatio) }}</strong>
          <span>{{ summary.failed_run_count }}/{{ summary.run_count }} runs · {{ formatTelemetryRatio(summary.failed_spend_ratio) }} spend</span>
        </article>
      </section>

      <section class="chart-grid">
        <div class="dimension-chart">
          <div class="dimension-control" aria-label="Token 拆分维度">
            <button
              v-for="dimension in dimensions"
              :key="dimension"
              type="button"
              :class="{ active: breakdownDimension === dimension }"
              :aria-pressed="breakdownDimension === dimension"
              @click="breakdownDimension = dimension"
            >
              {{ dimension }}
            </button>
          </div>
          <TelemetryBarChart
            :title="`Token by ${breakdownDimension}`"
            eyebrow="Usage"
            :items="breakdownItems"
            empty-message="当前维度没有可用 token 拆分。"
            primary-legend="Input"
            secondary-legend="Total"
          />
          <p v-if="breakdownItemCount > 12" class="breakdown-note">
            显示用量最高的 12 / {{ breakdownItemCount }} 项。
          </p>
        </div>
        <TelemetryBarChart
          title="Stage latency"
          eyebrow="Latency"
          :items="stageLatencyItems"
          empty-message="当前没有完成的 stage 延迟样本。"
          primary-legend="p50"
          secondary-legend="p95"
        />
        <div class="round-chart">
          <TelemetryBarChart
            title="Act rounds"
            eyebrow="Agent loop"
            :items="roundItems"
            empty-message="当前没有 act round 样本。"
            primary-legend="Runs"
          />
          <p v-if="summary.act.input_amplification_mean != null" class="amplification">
            平均 input amplification <strong>{{ summary.act.input_amplification_mean.toFixed(2) }}×</strong>
          </p>
        </div>
      </section>
    </template>

    <section class="runs-card card">
      <header class="runs-head">
        <div>
          <div class="eyebrow">Recent runs</div>
          <h2>最近运行</h2>
        </div>
        <div class="filters">
          <label>
            <span>类型</span>
            <select v-model="runKind" @change="resetRuns">
              <option value="">全部</option>
              <option value="tick">Tick</option>
              <option value="dream">Dream</option>
            </select>
          </label>
          <label>
            <span>状态</span>
            <select v-model="runStatus" @change="resetRuns">
              <option value="">全部</option>
              <option value="running">运行中</option>
              <option value="succeeded">成功</option>
              <option value="failed">失败</option>
              <option value="stale">疑似中断</option>
            </select>
          </label>
        </div>
      </header>

      <div v-if="runsLoading && !runs.length" class="state-msg">正在读取最近运行……</div>
      <div v-else-if="runsError && !runs.length" class="state-msg error">
        最近运行暂时无法读取。<button type="button" @click="resetRuns">重试</button>
      </div>
      <div v-else-if="!runs.length" class="state-msg">当前筛选条件下没有运行记录。</div>
      <div v-else class="run-list">
        <button
          v-for="run in runs"
          :key="run.run_id"
          type="button"
          class="run-row"
          :class="[{ selected: selectedRunId === run.run_id }, run.status]"
          :aria-expanded="selectedRunId === run.run_id && detail != null"
          @click="openDetail(run)"
        >
          <span class="run-kind">{{ telemetryRunKindLabel(run.run_kind) }}</span>
          <span class="run-identity">
            <strong>{{ run.tick_id != null ? `#${run.tick_id}` : (run.dream_date ?? run.trigger_source ?? 'run') }}</strong>
            <small>{{ formatTelemetryTime(run.started_at) }} · {{ run.trigger_source ?? run.execution_mode }}</small>
          </span>
          <span class="run-tokens">
            <strong>{{ formatTelemetryCount(run.tokens.total_tokens) }}</strong>
            <small :class="{ partial: run.total_available < run.request_count }">token · {{ run.total_available }}/{{ run.request_count }}</small>
          </span>
          <span class="run-duration">{{ formatTelemetryDuration(run.duration_us) }}</span>
          <span class="run-state">{{ telemetryStatusLabel(run.status) }}</span>
        </button>
      </div>

      <p v-if="runsError && runs.length" class="page-error">更早的运行暂时没有加载出来。</p>
      <button v-if="nextCursor" class="more" type="button" :disabled="runsLoading" @click="loadRuns(false)">
        {{ runsLoading ? '加载中…' : '加载更多' }}
      </button>
    </section>

    <div v-if="detailLoading" class="state-msg">正在展开安全 waterfall……</div>
    <div v-else-if="detailError" class="state-msg error">
      运行详情暂时无法读取。<button
        v-if="selectedRunId"
        type="button"
        @click="retryDetail"
      >重试</button>
    </div>
    <TelemetryRunWaterfall v-else-if="detail" :detail="detail" @close="closeDetail" />
  </div>
</template>

<style scoped>
.observability-view {
  display: grid;
  gap: 18px;
}
.page-head,
.runs-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}
.eyebrow {
  color: var(--text-faint);
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.page-head h1,
.runs-head h2 {
  margin: 2px 0 0;
  font-size: 1.14rem;
  font-weight: 600;
}
.page-head p {
  margin: 3px 0 0;
  color: var(--text-faint);
  font-size: 0.75rem;
}
.refresh,
.state-msg button {
  flex: none;
  padding: 7px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}
.refresh:hover:not(:disabled),
.state-msg button:hover {
  border-color: var(--accent-soft);
  color: var(--text);
}
.refresh:disabled {
  opacity: 0.45;
}
.window-control,
.dimension-control {
  display: inline-flex;
  width: fit-content;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
}
.window-control button,
.dimension-control button {
  padding: 6px 13px;
  border: 0;
  border-left: 1px solid var(--border);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 0.72rem;
}
.window-control button:first-child,
.dimension-control button:first-child {
  border-left: 0;
}
.window-control button.active,
.dimension-control button.active {
  background: var(--accent);
  color: white;
}
.state-msg {
  padding: 34px 20px;
}
.state-msg button {
  margin-left: 8px;
  padding: 4px 10px;
}
.warning-stack {
  display: grid;
  gap: 6px;
}
.warning-stack p {
  margin: 0;
  padding: 9px 12px;
  border: 1px solid rgba(201, 168, 106, 0.28);
  border-radius: 7px;
  background: rgba(201, 168, 106, 0.06);
  color: #d8c397;
  font-size: 0.72rem;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.metric-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  padding: 15px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-card);
  box-shadow: var(--shadow);
}
.metric-card small {
  color: var(--text-faint);
  font-size: 0.67rem;
  text-transform: uppercase;
}
.metric-card strong {
  margin: 5px 0 2px;
  color: var(--text);
  font-size: 1.25rem;
  font-variant-numeric: tabular-nums;
}
.metric-card span {
  overflow: hidden;
  color: var(--text-faint);
  font-size: 0.64rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chart-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.dimension-chart,
.round-chart {
  position: relative;
  min-width: 0;
}
.dimension-control {
  position: absolute;
  z-index: 2;
  top: 12px;
  right: 12px;
  background: var(--bg-card);
}
.dimension-control button {
  padding: 4px 7px;
  font-size: 0.61rem;
}
.amplification {
  position: absolute;
  right: 18px;
  bottom: 8px;
  margin: 0;
  color: var(--text-faint);
  font-size: 0.64rem;
}
.breakdown-note {
  margin: 7px 4px 0;
  color: var(--text-faint);
  font-size: 0.64rem;
  text-align: right;
}
.amplification strong {
  color: var(--gold);
}
.runs-card {
  padding: 20px;
}
.filters {
  display: flex;
  gap: 8px;
}
.filters label {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--text-faint);
  font-size: 0.66rem;
}
.filters select {
  padding: 5px 22px 5px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-card);
  color: var(--text-dim);
  font-size: 0.68rem;
}
.run-list {
  display: grid;
  gap: 6px;
  margin-top: 16px;
}
.run-row {
  display: grid;
  grid-template-columns: 52px minmax(120px, 1fr) 92px 78px 68px;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 11px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.018);
  color: inherit;
  cursor: pointer;
  text-align: left;
}
.run-row:hover,
.run-row.selected {
  border-color: var(--border);
  background: var(--bg-card-hover);
}
.run-row.failed,
.run-row.stale {
  border-left-color: var(--accent-soft);
}
.run-kind {
  color: var(--gold);
  font-size: 0.68rem;
  text-transform: uppercase;
}
.run-identity,
.run-tokens {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.run-identity strong,
.run-tokens strong {
  overflow: hidden;
  color: var(--text-dim);
  font-size: 0.73rem;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-identity small,
.run-tokens small {
  color: var(--text-faint);
  font-size: 0.62rem;
  font-variant-numeric: tabular-nums;
}
.run-tokens small.partial {
  color: var(--gold);
}
.run-duration,
.run-state {
  color: var(--text-dim);
  font-size: 0.68rem;
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.failed .run-state,
.stale .run-state {
  color: var(--accent-soft);
}
.page-error {
  margin: 12px 0 0;
  color: var(--accent-soft);
  font-size: 0.7rem;
  text-align: center;
}

@media (max-width: 760px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .chart-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 620px) {
  .page-head,
  .runs-head {
    align-items: flex-start;
  }
  .runs-head {
    flex-direction: column;
  }
  .filters {
    width: 100%;
  }
  .filters label {
    flex: 1;
  }
  .filters select {
    min-width: 0;
    flex: 1;
  }
  .runs-card {
    padding: 16px 12px;
  }
  .run-row {
    grid-template-columns: 44px minmax(90px, 1fr) 72px 58px;
  }
  .run-tokens {
    display: none;
  }
  .run-state {
    grid-column: 4;
  }
}
</style>
