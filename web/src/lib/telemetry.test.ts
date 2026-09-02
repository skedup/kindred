import { describe, expect, it } from 'vitest'
import type { TelemetrySummaryResponse } from '../api/types'
import {
  formatTelemetryDuration,
  formatTelemetryRatio,
  relativeTelemetryWidth,
  telemetryWarnings,
} from './telemetry'

function summary(): TelemetrySummaryResponse {
  return {
    schema_version: 1,
    window: '24h',
    from_at: '2026-08-25T00:00:00Z',
    to_at: '2026-08-26T00:00:00Z',
    empty: false,
    run_count: 2,
    succeeded_run_count: 1,
    failed_run_count: 1,
    running_run_count: 0,
    stale_run_count: 1,
    tokens: { input_tokens: 8, output_tokens: 2, total_tokens: 10 },
    coverage: {
      request_count: 4,
      total_available: 3,
      breakdown_available: 2,
      total_ratio: 0.75,
      breakdown_ratio: 0.5,
    },
    run_latency: { samples: 2, p50_us: 1_000, p95_us: 2_000, p99_us: 2_000 },
    stage_latency: { samples: 2, p50_us: 500, p95_us: 900, p99_us: 900 },
    stage_latencies: [],
    breakdowns: [],
    breakdowns_truncated: true,
    cache_read_ratio: null,
    failed_spend_ratio: 0.2,
    act: { round_distribution: [], input_amplification_mean: null },
    budget: {
      daily_token_warn: 9,
      daily_token_exceeded: true,
      tick_duration_warn_seconds: null,
      tick_duration_exceeded: null,
      dream_duration_warn_seconds: null,
      dream_duration_exceeded: null,
    },
  }
}

describe('telemetry formatting', () => {
  it('uses readable units without inventing missing durations', () => {
    expect(formatTelemetryDuration(null)).toBe('—')
    expect(formatTelemetryDuration(999)).toBe('999 µs')
    expect(formatTelemetryDuration(1_500)).toBe('1.5 ms')
    expect(formatTelemetryDuration(2_500_000)).toBe('2.5 s')
    expect(formatTelemetryRatio(0.125)).toBe('12.5%')
  })

  it('keeps non-zero bars visible and clamps their width', () => {
    expect(relativeTelemetryWidth(null, 10)).toBe(0)
    expect(relativeTelemetryWidth(1, 1_000)).toBe(3)
    expect(relativeTelemetryWidth(20, 10)).toBe(100)
  })

  it('makes partial, truncated, stale and budget states explicit', () => {
    expect(telemetryWarnings(summary())).toEqual([
      '只有 75% 的请求具备 total token，汇总值是已知下界。',
      '只有 50% 的请求具备完整 input/output 拆分。',
      'Provider 或模型维度已达到展示上限，图表只显示最高用量分组。',
      '1 个运行超过 24 小时未结束，当前仅派生为疑似中断。',
      '24 小时 token 已超过配置阈值。',
    ])
  })

  it('warns when configured budgets cannot be evaluated', () => {
    const current = summary()
    current.coverage = {
      request_count: 2,
      total_available: 2,
      breakdown_available: 2,
      total_ratio: 1,
      breakdown_ratio: 1,
    }
    current.breakdowns_truncated = false
    current.stale_run_count = 0
    current.budget = {
      daily_token_warn: 20,
      daily_token_exceeded: null,
      tick_duration_warn_seconds: 3,
      tick_duration_exceeded: null,
      dream_duration_warn_seconds: 30,
      dream_duration_exceeded: null,
    }

    expect(telemetryWarnings(current)).toEqual([
      '24 小时 token 数据不足，暂时无法判断是否超过配置阈值。',
      'Tick p95 暂无足够样本，无法判断是否超过配置阈值。',
      'Dream p95 暂无足够样本，无法判断是否超过配置阈值。',
    ])
  })
})
