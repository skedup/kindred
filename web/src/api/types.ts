// 对外契约类型 —— 与后端 src/kindred/web/contracts.py 一一对齐。
//
// 后端用 pydantic 强类型吐 JSON，前端这里镜像同一形状。任何一边改契约，
// 两边都要同步改（与后端 typed-helper 铁律同源：契约是前后端的唯一真相）。

/** 带描述的 0-100 表盘（body / mood / inner_pulse）。 */
export interface Gauge {
  value: number
  description: string
}

/** 此刻穿戴（外观层）。 */
export interface Outfit {
  top: string
  bottom: string
  outer: string | null
  shoes: string
  accessory: string[]
  makeup: string | null
}

/** 私密层（内衣+袜）。数据边界 gate（N-6）：明文仅 revealed 时返回。 */
export interface Intimate {
  revealed: boolean
  tease: string
  bra: string | null
  panties: string | null
  socks: string | null
}

/** 心里的一个念头。 */
export interface ThoughtView {
  description: string
  tag: string
  mood_w: number
}

/** 叙事视图 —— 面向 companion user：ta 此刻在做什么 + 心情 + 在哪 + 穿什么 + 身边有谁。 */
export interface NarrativeView {
  activity_name: string
  activity_desc: string
  activity_for_what: string
  activity_step: string | null
  engagement: number
  with_whom: string[]
  location_name: string
  location_type: string | null
  mood_note: string
  note: string | null
  thoughts: ThoughtView[]
  outfit: Outfit
  intimate: Intimate
  bag_items: string[]
  time_phase: string
  weekday: string
  user_present: boolean
  others_present: string[]
  city: string
  weather: string
  temperature: number | null
  ambience: string
}

/** 指标视图 —— 面向 engineer user：7 needs + 4 affect + 3 gauge + significance。 */
export interface MetricsView {
  body: Gauge
  mood: Gauge
  inner_pulse: Gauge
  needs: Record<string, number>
  affect: Record<string, number>
  significance: number | null
}

/** GET /now 响应 —— 此刻切片。 */
export interface NowResponse {
  ts: string | null
  trigger_source: string | null
  narrative: NarrativeView
  metrics: MetricsView
  empty: boolean
}

export type RelationshipRole = 'unlabeled' | 'friend' | 'lover' | 'hostile'

/** 当前 Relationship；可信 loopback 观察面展示原始四轴。 */
export interface RelationshipView {
  declared_role: RelationshipRole
  trust: number
  attachment: number
  attraction: number
  friction: number
}

/** 生命流 / 高光闪回的一条 —— 一个 tick 的轻量摘要。 */
export interface StreamItem {
  id: number
  ts: string | null
  note: string | null
  significance: number | null
  target_activity: string | null
}

/** GET /stream | /episodes 响应 —— 一页轻量 tick（按 id DESC）。 */
export interface StreamResponse {
  items: StreamItem[]
  next_cursor: number | null
  empty: boolean
}

/** 单个 tick 的 interior 历史数值；null 在图上表现为断点。 */
export interface InteriorHistoryValues {
  body: number | null
  mood: number | null
  inner_pulse: number | null
  needs: Record<string, number | null>
  affect: Record<string, number | null>
}

/** 趋势图的一点，附带 hover 所需的少量 tick 信息。 */
export interface InteriorHistoryPoint {
  id: number
  ts: string | null
  trigger_source: string | null
  activity_name: string
  activity_step: string | null
  note: string | null
  significance: number | null
  values: InteriorHistoryValues
}

/** GET /interior/history 响应，points 按 tick id 正序。 */
export interface InteriorHistoryResponse {
  points: InteriorHistoryPoint[]
  empty: boolean
}

export type ArtifactKind = 'text' | 'image' | 'mixed' | 'files'
export type ArtifactAvailability = 'available' | 'partial' | 'unavailable'
export type ArtifactMemberRole = 'title' | 'content' | 'image' | 'file'
export type ArtifactMemberAvailability = 'available' | 'unavailable' | 'unsupported'

/** 作品流中的 committed Artifact 摘要。 */
export interface ArtifactListItem {
  tick_id: number
  artifact_ordinal: number
  ts: string
  activity_name: string
  kind: ArtifactKind
  label: string
  member_count: number
  total_bytes: number
  availability: ArtifactAvailability
}

/** 展开作品后可见的安全成员描述。 */
export interface ArtifactMemberView {
  member_ordinal: number
  role: ArtifactMemberRole
  media_type: string
  bytes: number
  availability: ArtifactMemberAvailability
}

export interface ArtifactDetailResponse extends ArtifactListItem {
  members: ArtifactMemberView[]
}

export interface ArtifactListResponse {
  items: ArtifactListItem[]
  next_cursor: string | null
}

/** GET /healthz 响应。 */
export interface HealthResponse {
  status: string
  db_exists: boolean
}

export type TelemetryWindow = '24h' | '7d' | '30d'
export type TelemetryRunKind = 'tick' | 'dream'
export type TelemetryRunStatus = 'running' | 'succeeded' | 'failed' | 'stale'
export type TelemetryStoredStatus = Exclude<TelemetryRunStatus, 'stale'>

export interface TelemetryTokenTotals {
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

export interface TelemetryLatencyPercentiles {
  samples: number
  p50_us: number | null
  p95_us: number | null
  p99_us: number | null
}

export interface TelemetryUsageCoverage {
  request_count: number
  total_available: number
  breakdown_available: number
  total_ratio: number
  breakdown_ratio: number
}

export interface TelemetryUsageBreakdown {
  dimension: 'role' | 'provider' | 'model'
  key: string
  request_count: number
  tokens: TelemetryTokenTotals
}

export interface TelemetryStageLatency {
  name: string
  latency: TelemetryLatencyPercentiles
}

export interface TelemetryActRoundBucket {
  rounds: number
  run_count: number
}

export interface TelemetryActMetrics {
  round_distribution: TelemetryActRoundBucket[]
  input_amplification_mean: number | null
}

export interface TelemetryBudgetStatus {
  daily_token_warn: number | null
  daily_token_exceeded: boolean | null
  tick_duration_warn_seconds: number | null
  tick_duration_exceeded: boolean | null
  dream_duration_warn_seconds: number | null
  dream_duration_exceeded: boolean | null
}

export interface TelemetrySummaryResponse {
  schema_version: 1
  window: TelemetryWindow
  from_at: string
  to_at: string
  empty: boolean
  run_count: number
  succeeded_run_count: number
  failed_run_count: number
  running_run_count: number
  stale_run_count: number
  tokens: TelemetryTokenTotals
  coverage: TelemetryUsageCoverage
  run_latency: TelemetryLatencyPercentiles
  stage_latency: TelemetryLatencyPercentiles
  stage_latencies: TelemetryStageLatency[]
  breakdowns: TelemetryUsageBreakdown[]
  breakdowns_truncated: boolean
  cache_read_ratio: number | null
  failed_spend_ratio: number | null
  act: TelemetryActMetrics
  budget: TelemetryBudgetStatus
}

export interface TelemetryRunItem {
  run_id: string
  run_kind: TelemetryRunKind
  execution_mode: 'real' | 'mock'
  trigger_source: 'cold_start' | 'heartbeat' | 'watcher' | null
  dream_date: string | null
  tick_id: number | null
  started_at: string
  ended_at: string | null
  duration_us: number | null
  status: TelemetryRunStatus
  error_type: string | null
  app_version: string
  span_count: number
  request_count: number
  total_available: number
  tokens: TelemetryTokenTotals
}

export interface TelemetryRunListResponse {
  schema_version: 1
  empty: boolean
  items: TelemetryRunItem[]
  next_cursor: string | null
}

export interface TelemetryUsageDetail {
  usage_source: 'provider_complete' | 'provider_partial' | 'unavailable'
  reconciliation_status: 'exact' | 'provider_total_only' | 'partial' | 'mismatch' | 'unavailable'
  base_input_tokens: number | null
  visible_output_tokens: number | null
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  total_derived: boolean
  cache_read_input_tokens: number | null
  cache_write_input_tokens: number | null
  reasoning_output_tokens: number | null
  tool_use_prompt_tokens: number | null
  unattributed_tokens: number | null
  provider_cost_microusd: number | null
  payload_json_bytes: number | null
  system_text_chars: number | null
  initial_user_text_chars: number | null
  tool_schema_json_chars: number | null
  response_schema_json_chars: number | null
  model_history_json_chars: number | null
  tool_result_json_chars: number | null
}

export interface TelemetrySpanDetail {
  span_id: string
  parent_span_id: string | null
  sequence: number
  span_kind: 'graph_node' | 'llm_request' | 'tool'
  name: string
  llm_role:
    | 'sense.llm'
    | 'act.llm'
    | 'dream.summarize'
    | 'dream.reflect'
    | 'dream.gate'
    | 'dream.excerpt'
    | null
  round_index: number | null
  source_round_index: number | null
  tool_effect:
    | 'read_only'
    | 'staged_state_event'
    | 'external_side_effect'
    | 'artifact_write'
    | null
  provider: string | null
  requested_model: string | null
  response_model: string | null
  started_at: string
  ended_at: string | null
  duration_us: number | null
  status: TelemetryStoredStatus
  error_type: string | null
  http_status: number | null
  usage: TelemetryUsageDetail | null
}

export interface TelemetryStageTree {
  stage: TelemetrySpanDetail
  children: TelemetrySpanDetail[]
}

export interface TelemetryRunDetailResponse {
  schema_version: 1
  run: TelemetryRunItem
  stages: TelemetryStageTree[]
}
