// @vitest-environment jsdom

import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  TelemetryRunDetailResponse,
  TelemetryRunItem,
  TelemetrySummaryResponse,
  TelemetryUsageDetail,
} from '../api/types'
import ObservabilityView from './ObservabilityView.vue'

const RUN_ID = '00000000-0000-4000-8000-000000000001'
const STAGE_ID = '10000000-0000-4000-8000-000000000001'
const REQUEST_ID = '20000000-0000-4000-8000-000000000001'

function summary(empty = false): TelemetrySummaryResponse {
  return {
    schema_version: 1,
    window: '24h',
    from_at: '2026-08-26T00:00:00Z',
    to_at: '2026-08-27T00:00:00Z',
    empty,
    run_count: empty ? 0 : 3,
    succeeded_run_count: empty ? 0 : 2,
    failed_run_count: empty ? 0 : 1,
    running_run_count: 0,
    stale_run_count: 0,
    tokens: { input_tokens: 1_200, output_tokens: 300, total_tokens: 1_500 },
    coverage: {
      request_count: empty ? 0 : 4,
      total_available: empty ? 0 : 3,
      breakdown_available: empty ? 0 : 3,
      total_ratio: empty ? 0 : 0.75,
      breakdown_ratio: empty ? 0 : 0.75,
    },
    run_latency: { samples: empty ? 0 : 3, p50_us: 1_000_000, p95_us: 2_500_000, p99_us: 2_500_000 },
    stage_latency: { samples: empty ? 0 : 6, p50_us: 500_000, p95_us: 900_000, p99_us: 900_000 },
    stage_latencies: empty
      ? []
      : [{ name: 'T1.sense.llm', latency: { samples: 3, p50_us: 500_000, p95_us: 900_000, p99_us: 900_000 } }],
    breakdowns: empty
      ? []
      : [
          { dimension: 'role', key: 'act.llm', request_count: 2, tokens: { input_tokens: 800, output_tokens: 200, total_tokens: 1_000 } },
          { dimension: 'provider', key: 'openai', request_count: 4, tokens: { input_tokens: 1_200, output_tokens: 300, total_tokens: 1_500 } },
          { dimension: 'model', key: 'gpt-test', request_count: 4, tokens: { input_tokens: 1_200, output_tokens: 300, total_tokens: 1_500 } },
        ],
    breakdowns_truncated: false,
    cache_read_ratio: 0.25,
    failed_spend_ratio: 0.2,
    act: {
      round_distribution: [{ rounds: 2, run_count: 2 }],
      input_amplification_mean: 1.5,
    },
    budget: {
      daily_token_warn: null,
      daily_token_exceeded: null,
      tick_duration_warn_seconds: null,
      tick_duration_exceeded: null,
      dream_duration_warn_seconds: null,
      dream_duration_exceeded: null,
    },
  }
}

function run(id = RUN_ID): TelemetryRunItem {
  return {
    run_id: id,
    run_kind: 'tick',
    execution_mode: 'real',
    trigger_source: 'heartbeat',
    dream_date: null,
    tick_id: 42,
    started_at: '2026-08-26T12:00:00Z',
    ended_at: '2026-08-26T12:00:03Z',
    duration_us: 3_000_000,
    status: 'failed',
    error_type: 'ProviderError',
    app_version: 'test',
    span_count: 2,
    request_count: 1,
    total_available: 1,
    tokens: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
  }
}

function usage(): TelemetryUsageDetail {
  return {
    usage_source: 'provider_complete',
    reconciliation_status: 'exact',
    base_input_tokens: 10,
    visible_output_tokens: 5,
    input_tokens: 10,
    output_tokens: 5,
    total_tokens: 15,
    total_derived: false,
    cache_read_input_tokens: 2,
    cache_write_input_tokens: null,
    reasoning_output_tokens: 1,
    tool_use_prompt_tokens: null,
    unattributed_tokens: null,
    provider_cost_microusd: 25,
    payload_json_bytes: 512,
    system_text_chars: 120,
    initial_user_text_chars: 40,
    tool_schema_json_chars: 80,
    response_schema_json_chars: null,
    model_history_json_chars: 200,
    tool_result_json_chars: null,
  }
}

function detail(): TelemetryRunDetailResponse {
  return {
    schema_version: 1,
    run: run(),
    stages: [
      {
        stage: {
          span_id: STAGE_ID,
          parent_span_id: null,
          sequence: 1,
          span_kind: 'graph_node',
          name: 'T1.sense.llm',
          llm_role: null,
          round_index: null,
          source_round_index: null,
          tool_effect: null,
          provider: null,
          requested_model: null,
          response_model: null,
          started_at: '2026-08-26T12:00:00Z',
          ended_at: '2026-08-26T12:00:01Z',
          duration_us: 1_000_000,
          status: 'failed',
          error_type: 'ProviderError',
          http_status: null,
          usage: null,
        },
        children: [
          {
            span_id: REQUEST_ID,
            parent_span_id: STAGE_ID,
            sequence: 2,
            span_kind: 'llm_request',
            name: 'sense.llm',
            llm_role: 'sense.llm',
            round_index: 1,
            source_round_index: null,
            tool_effect: null,
            provider: 'openai',
            requested_model: 'gpt-test',
            response_model: 'gpt-test-v2',
            started_at: '2026-08-26T12:00:00Z',
            ended_at: '2026-08-26T12:00:00.8Z',
            duration_us: 800_000,
            status: 'succeeded',
            error_type: null,
            http_status: 200,
            usage: usage(),
          },
        ],
      },
    ],
  }
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function mount(): HTMLElement {
  const root = document.createElement('div')
  document.body.append(root)
  createApp(ObservabilityView).mount(root)
  return root
}

function defaultFetch(input: string | URL | Request): Response {
  const url = String(input)
  if (url.startsWith('/api/observability/summary')) return Response.json(summary())
  if (url === '/api/observability/runs?limit=20') {
    return Response.json({ schema_version: 1, empty: false, items: [run()], next_cursor: 'signed/+opaque==' })
  }
  if (url === `/api/observability/runs/${RUN_ID}`) return Response.json(detail())
  if (url.includes('cursor=signed%2F%2B%3D%3D')) {
    return Response.json({ schema_version: 1, empty: true, items: [], next_cursor: null })
  }
  if (url.includes('status=succeeded')) {
    return Response.json({ schema_version: 1, empty: true, items: [], next_cursor: null })
  }
  throw new Error(`unexpected request: ${url}`)
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('ObservabilityView', () => {
  it('renders summary cards, partial warnings and switchable safe breakdowns', async () => {
    const fetch = vi.fn((input: string | URL | Request) => Promise.resolve(defaultFetch(input)))
    vi.stubGlobal('fetch', fetch)
    const root = mount()
    await settle()

    expect(root.textContent).toContain('1,500')
    expect(root.textContent).toContain('7d p95 2.5 s')
    expect(root.textContent).toContain('75% 的请求具备 total token')
    expect(root.textContent).toContain('act.llm')
    const provider = [...root.querySelectorAll<HTMLButtonElement>('.dimension-control button')].find(
      (button) => button.textContent?.trim() === 'provider',
    )!
    provider.click()
    await nextTick()
    expect(root.textContent).toContain('openai')
    expect(root.querySelector('.bars')?.getAttribute('role')).toBe('list')
    expect(root.querySelectorAll('[role="listitem"]').length).toBeGreaterThan(0)
    expect(root.querySelector('[role="img"]')).toBeNull()

    const sevenDays = [...root.querySelectorAll<HTMLButtonElement>('.window-control button')].find(
      (button) => button.textContent?.includes('7 天'),
    )!
    sevenDays.click()
    await settle()
    expect(fetch).toHaveBeenCalledWith(
      '/api/observability/summary?window=7d',
      expect.objectContaining({ headers: { Accept: 'application/json' } }),
    )
  })

  it('discloses the client-side Top 12 breakdown limit', async () => {
    const grouped = summary()
    grouped.breakdowns = Array.from({ length: 13 }, (_, index) => ({
      dimension: 'provider' as const,
      key: `provider-${index + 1}`,
      request_count: 1,
      tokens: { input_tokens: 13 - index, output_tokens: 1, total_tokens: 14 - index },
    }))
    grouped.breakdowns_truncated = false
    const fetch = vi.fn((input: string | URL | Request) => {
      const url = String(input)
      if (url.startsWith('/api/observability/summary')) {
        return Promise.resolve(Response.json(grouped))
      }
      return Promise.resolve(defaultFetch(input))
    })
    vi.stubGlobal('fetch', fetch)
    const root = mount()
    await settle()

    const provider = [...root.querySelectorAll<HTMLButtonElement>('.dimension-control button')].find(
      (button) => button.textContent?.trim() === 'provider',
    )!
    provider.click()
    await nextTick()

    expect(root.querySelectorAll('.dimension-chart .bar-row')).toHaveLength(12)
    expect(root.textContent).toContain('显示用量最高的 12 / 13 项')
    expect(root.textContent).not.toContain('已达到展示上限')
  })

  it('does not label an old summary as a newly selected window', async () => {
    const thirtyDays = deferred<Response>()
    const fetch = vi.fn((input: string | URL | Request) => {
      const url = String(input)
      if (url === '/api/observability/summary?window=30d') return thirtyDays.promise
      return Promise.resolve(defaultFetch(input))
    })
    vi.stubGlobal('fetch', fetch)
    const root = mount()
    await settle()

    const thirtyDayButton = [...root.querySelectorAll<HTMLButtonElement>('.window-control button')].find(
      (button) => button.textContent?.includes('30 天'),
    )!
    thirtyDayButton.click()
    await nextTick()

    expect(root.textContent).toContain('正在汇总本地运行数据')
    expect(root.textContent).not.toContain('1,500')

    const response = summary()
    response.window = '30d'
    response.tokens = { input_tokens: 2_400, output_tokens: 600, total_tokens: 3_000 }
    thirtyDays.resolve(Response.json(response))
    await settle()
    expect(root.textContent).toContain('3,000')
  })

  it('ignores an older baseline response after refresh', async () => {
    const firstBaseline = deferred<Response>()
    const secondBaseline = deferred<Response>()
    let baselineCalls = 0
    const fetch = vi.fn((input: string | URL | Request) => {
      const url = String(input)
      if (url === '/api/observability/summary?window=7d') {
        baselineCalls += 1
        return baselineCalls === 1 ? firstBaseline.promise : secondBaseline.promise
      }
      return Promise.resolve(defaultFetch(input))
    })
    vi.stubGlobal('fetch', fetch)
    const root = mount()
    await settle()

    root.querySelector<HTMLButtonElement>('button.refresh')!.click()
    await nextTick()

    const fresh = summary()
    fresh.window = '7d'
    fresh.run_latency.p95_us = 7_000_000
    secondBaseline.resolve(Response.json(fresh))
    await settle()
    expect(root.textContent).toContain('7d p95 7 s')

    const stale = summary()
    stale.window = '7d'
    stale.run_latency.p95_us = 9_000_000
    firstBaseline.resolve(Response.json(stale))
    await settle()
    expect(root.textContent).toContain('7d p95 7 s')
    expect(root.textContent).not.toContain('7d p95 9 s')
  })

  it('paginates opaque cursors, filters runs and expands numeric waterfall detail', async () => {
    const fetch = vi.fn((input: string | URL | Request) => Promise.resolve(defaultFetch(input)))
    vi.stubGlobal('fetch', fetch)
    const root = mount()
    await settle()

    root.querySelector<HTMLButtonElement>('.run-row')!.click()
    await settle()
    expect(root.textContent).toContain('横条只比较各 span 自身 duration')
    const spanRows = root.querySelectorAll<HTMLButtonElement>('.span-toggle')
    spanRows[1]!.click()
    await nextTick()
    expect(root.textContent).toContain('gpt-test-v2')
    expect(root.textContent).toContain('Cache read')
    expect(root.textContent).toContain('$0.000025')

    root.querySelector<HTMLButtonElement>('button.more')!.click()
    await settle()
    expect(fetch).toHaveBeenCalledWith(
      '/api/observability/runs?cursor=signed%2F%2Bopaque%3D%3D&limit=20',
      expect.any(Object),
    )

    const status = root.querySelectorAll<HTMLSelectElement>('.filters select')[1]!
    status.value = 'succeeded'
    status.dispatchEvent(new Event('change'))
    await settle()
    expect(fetch).toHaveBeenCalledWith(
      '/api/observability/runs?limit=20&status=succeeded',
      expect.any(Object),
    )
  })

  it('separates empty summary and unavailable run states', async () => {
    const fetch = vi.fn((input: string | URL | Request) => {
      const url = String(input)
      if (url.startsWith('/api/observability/summary')) return Promise.resolve(Response.json(summary(true)))
      return Promise.reject(new Error('offline'))
    })
    vi.stubGlobal('fetch', fetch)
    const root = mount()
    await settle()

    expect(root.textContent).toContain('当前时间窗还没有 real tick 或 dream')
    expect(root.textContent).toContain('最近运行暂时无法读取')
  })
})
