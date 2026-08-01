// 只读 HTTP 客户端 —— 调后端观察端点 + healthz。
//
// 全部走 /api 前缀：dev 由 vite proxy 转到 kindred serve；生产同源部署时
// 后端把前端 dist 挂在同源下，/api 也成立（后续 PR 接静态托管）。
//
// 后端契约「绝不 500」：空库 / 缺字段都降级成 empty=true，所以这里只处理
// 网络层 / HTTP 错误，业务空态交给各 view 按 empty 渲染。

import type {
  ArtifactDetailResponse,
  ArtifactListResponse,
  HealthResponse,
  InteriorHistoryResponse,
  NowResponse,
  StreamResponse,
} from './types'

const BASE = '/api'

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { Accept: 'application/json' },
  })
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} ${resp.statusText} @ ${path}`)
  }
  return (await resp.json()) as T
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>('/healthz')
}

/** 此刻切片。reveal=true 请求揭示私密层（揭示开关）；后端按 ?reveal=1 OR 亲密度够 决定是否出明文。 */
export function fetchNow(params?: { reveal?: boolean }): Promise<NowResponse> {
  const q = params?.reveal ? '?reveal=1' : ''
  return getJson<NowResponse>(`/now${q}`)
}

/** 生命流：全量 tick，cursor 分页。before 缺省=最新一页。 */
export function fetchStream(params?: { before?: number; limit?: number }): Promise<StreamResponse> {
  return getJson<StreamResponse>(`/stream${buildQuery(params)}`)
}

/** 高光闪回：significance>=7，cursor 分页。 */
export function fetchEpisodes(params?: {
  before?: number
  limit?: number
}): Promise<StreamResponse> {
  return getJson<StreamResponse>(`/episodes${buildQuery(params)}`)
}

/** Interior 数值历史：最近 N 个 tick，按 id 正序。 */
export function fetchInteriorHistory(limit = 120): Promise<InteriorHistoryResponse> {
  return getJson<InteriorHistoryResponse>(`/interior/history?limit=${limit}`)
}

/** committed 作品流。cursor 是 Host 签发的 opaque string，前端只负责原样回传。 */
export function fetchArtifacts(
  params?: { cursor?: string; limit?: number },
): Promise<ArtifactListResponse> {
  return getJson<ArtifactListResponse>(`/artifacts${buildQuery(params)}`)
}

export function fetchArtifactDetail(tickId: number, ordinal: number): Promise<ArtifactDetailResponse> {
  return getJson<ArtifactDetailResponse>(artifactPath(tickId, ordinal))
}

export async function fetchArtifactText(
  tickId: number,
  ordinal: number,
  member: number,
): Promise<string> {
  const path = artifactPath(tickId, ordinal, member)
  const response = await fetch(`${BASE}${path}`, { headers: { Accept: 'text/*' } })
  if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText} @ ${path}`)
  return response.text()
}

export function artifactMemberUrl(tickId: number, ordinal: number, member: number): string {
  return `${BASE}${artifactPath(tickId, ordinal, member)}`
}

function buildQuery(params?: { before?: number; cursor?: string; limit?: number }): string {
  if (!params) return ''
  const q = new URLSearchParams()
  if (params.before != null) q.set('before', String(params.before))
  if (params.cursor != null) q.set('cursor', params.cursor)
  if (params.limit != null) q.set('limit', String(params.limit))
  const s = q.toString()
  return s ? `?${s}` : ''
}

function artifactPath(tickId: number, ordinal: number, member?: number): string {
  const base = `/artifacts/${tickId}/${ordinal}`
  return member == null ? base : `${base}/members/${member}`
}
