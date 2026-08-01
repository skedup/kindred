// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import type { ArtifactListItem } from '../api/types'
import {
  appendArtifactPage,
  artifactKindLabel,
  formatArtifactBytes,
  renderArtifactMarkdown,
} from './artifacts'

function item(tickId: number, ordinal = 0): ArtifactListItem {
  return {
    tick_id: tickId,
    artifact_ordinal: ordinal,
    ts: '2026-07-29T12:00:00+08:00',
    activity_name: 'draw_something',
    kind: 'text',
    label: '作品',
    member_count: 1,
    total_bytes: 12,
    availability: 'available',
  }
}

describe('artifact presentation helpers', () => {
  it('appends cursor pages without duplicating an overlapping item', () => {
    expect(appendArtifactPage([item(3), item(2)], [item(2), item(1)])).toEqual([
      item(3),
      item(2),
      item(1),
    ])
  })

  it('keeps generic kind labels and readable byte sizes', () => {
    expect(artifactKindLabel).toEqual({
      text: '文字',
      image: '图片',
      mixed: '图文',
      files: '文件',
    })
    expect(formatArtifactBytes(1024)).toBe('1.0 KiB')
  })

  it('sanitizes raw HTML while preserving ordinary markdown', () => {
    const html = renderArtifactMarkdown(
      '# 标题\n\n[链接](https://example.com)<script>bad()</script><img src=x onerror=bad()>',
    )
    expect(html).toContain('<h1>标题</h1>')
    expect(html).toContain('https://example.com')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('onerror')
  })
})
