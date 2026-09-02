import type {
  TelemetryRunKind,
  TelemetryRunStatus,
  TelemetrySpanDetail,
  TelemetrySummaryResponse,
} from '../api/types'

const integer = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })
const decimal = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 })

export function formatTelemetryCount(value: number | null | undefined): string {
  return value == null ? '—' : integer.format(value)
}

export function formatTelemetryDuration(durationUs: number | null | undefined): string {
  if (durationUs == null) return '—'
  if (durationUs < 1_000) return `${integer.format(durationUs)} µs`
  if (durationUs < 1_000_000) return `${decimal.format(durationUs / 1_000)} ms`
  return `${decimal.format(durationUs / 1_000_000)} s`
}

export function formatTelemetryRatio(value: number | null | undefined): string {
  return value == null ? '—' : `${decimal.format(value * 100)}%`
}

export function formatTelemetryTime(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function telemetryRunKindLabel(kind: TelemetryRunKind): string {
  return kind === 'tick' ? 'Tick' : 'Dream'
}

export function telemetryStatusLabel(status: TelemetryRunStatus): string {
  return {
    running: '运行中',
    succeeded: '成功',
    failed: '失败',
    stale: '疑似中断',
  }[status]
}

export function telemetrySpanKindLabel(span: TelemetrySpanDetail): string {
  return {
    graph_node: 'Stage',
    llm_request: 'LLM',
    tool: 'Tool',
  }[span.span_kind]
}

export function telemetryWarnings(summary: TelemetrySummaryResponse): string[] {
  const warnings: string[] = []
  if (summary.coverage.request_count > 0 && summary.coverage.total_ratio < 1) {
    warnings.push(
      `只有 ${formatTelemetryRatio(summary.coverage.total_ratio)} 的请求具备 total token，汇总值是已知下界。`,
    )
  }
  if (summary.coverage.request_count > 0 && summary.coverage.breakdown_ratio < 1) {
    warnings.push(
      `只有 ${formatTelemetryRatio(summary.coverage.breakdown_ratio)} 的请求具备完整 input/output 拆分。`,
    )
  }
  if (summary.breakdowns_truncated) {
    warnings.push('Provider 或模型维度已达到展示上限，图表只显示最高用量分组。')
  }
  if (summary.stale_run_count > 0) {
    warnings.push(`${summary.stale_run_count} 个运行超过 24 小时未结束，当前仅派生为疑似中断。`)
  }
  if (summary.budget.daily_token_exceeded) warnings.push('24 小时 token 已超过配置阈值。')
  else if (
    summary.window === '24h' &&
    summary.budget.daily_token_warn != null &&
    summary.budget.daily_token_exceeded == null
  ) {
    warnings.push('24 小时 token 数据不足，暂时无法判断是否超过配置阈值。')
  }
  if (summary.budget.tick_duration_exceeded) warnings.push('Tick p95 延迟已超过配置阈值。')
  else if (
    summary.budget.tick_duration_warn_seconds != null &&
    summary.budget.tick_duration_exceeded == null
  ) {
    warnings.push('Tick p95 暂无足够样本，无法判断是否超过配置阈值。')
  }
  if (summary.budget.dream_duration_exceeded) warnings.push('Dream p95 延迟已超过配置阈值。')
  else if (
    summary.budget.dream_duration_warn_seconds != null &&
    summary.budget.dream_duration_exceeded == null
  ) {
    warnings.push('Dream p95 暂无足够样本，无法判断是否超过配置阈值。')
  }
  return warnings
}

export function relativeTelemetryWidth(value: number | null, maximum: number): number {
  if (value == null || value <= 0 || maximum <= 0) return 0
  return Math.max(3, Math.min(100, (value / maximum) * 100))
}
