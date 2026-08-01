import DOMPurify from 'dompurify'
import { marked } from 'marked'
import type {
  ArtifactAvailability, ArtifactKind, ArtifactListItem, ArtifactMemberAvailability,
} from '../api/types'

export const artifactKindLabel: Record<ArtifactKind, string> = {
  text: '文字',
  image: '图片',
  mixed: '图文',
  files: '文件',
}

export const artifactAvailabilityLabel: Record<ArtifactAvailability, string> = {
  available: '可查看',
  partial: '部分可用',
  unavailable: '暂不可用',
}

export const memberAvailabilityLabel: Record<ArtifactMemberAvailability, string> = {
  available: '可查看',
  unavailable: '不可用',
  unsupported: '暂不支持',
}

export function artifactKey(item: Pick<ArtifactListItem, 'tick_id' | 'artifact_ordinal'>): string {
  return `${item.tick_id}:${item.artifact_ordinal}`
}

export function appendArtifactPage(
  current: ArtifactListItem[],
  incoming: ArtifactListItem[],
): ArtifactListItem[] {
  const seen = new Set(current.map(artifactKey))
  return [...current, ...incoming.filter((item) => !seen.has(artifactKey(item)))]
}

export function formatArtifactBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

export function formatArtifactTime(ts: string): string {
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString('zh-CN', { hour12: false })
}

/** Markdown 是 Artifact 正文，不是可信 HTML；唯一 HTML 出口必须经过 DOMPurify。 */
export function renderArtifactMarkdown(source: string): string {
  const rendered = marked.parse(source, { async: false }) as string
  return DOMPurify.sanitize(rendered)
}
