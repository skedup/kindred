// @vitest-environment jsdom

import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ArtifactDetailResponse, ArtifactListItem } from '../api/types'
import WorksView from './WorksView.vue'

function item(
  tickId: number,
  kind: ArtifactListItem['kind'],
  availability: ArtifactListItem['availability'] = 'available',
): ArtifactListItem {
  return {
    tick_id: tickId,
    artifact_ordinal: 0,
    ts: '2026-07-29T12:00:00+08:00',
    activity_name: 'draw_something',
    kind,
    label: tickId === 4 ? '自定义作品' : `${kind} work`,
    member_count: kind === 'mixed' ? 3 : 1,
    total_bytes: 2048,
    availability,
  }
}

const listed = [
  item(5, 'text'),
  item(4, 'mixed'),
  item(3, 'image'),
  item(2, 'files'),
  item(1, 'files', 'unavailable'),
]

const details: Record<number, ArtifactDetailResponse> = {
  5: {
    ...listed[0]!,
    members: [{ member_ordinal: 0, role: 'content', media_type: 'text/plain', bytes: 20, availability: 'available' }],
  },
  4: {
    ...listed[1]!,
    members: [
      { member_ordinal: 0, role: 'title', media_type: 'text/plain', bytes: 5, availability: 'available' },
      { member_ordinal: 1, role: 'content', media_type: 'text/markdown', bytes: 20, availability: 'available' },
      { member_ordinal: 2, role: 'image', media_type: 'image/png', bytes: 2000, availability: 'available' },
    ],
  },
  3: {
    ...listed[2]!,
    members: [{ member_ordinal: 0, role: 'image', media_type: 'image/webp', bytes: 2048, availability: 'available' }],
  },
  2: {
    ...listed[3]!,
    members: [{ member_ordinal: 0, role: 'file', media_type: 'text/html', bytes: 30, availability: 'unsupported' }],
  },
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

function mount(): { root: HTMLElement; unmount: () => void } {
  const root = document.createElement('div')
  document.body.append(root)
  const app = createApp(WorksView)
  app.mount(root)
  return { root, unmount: () => app.unmount() }
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('WorksView', () => {
  it('loads metadata first and renders text, image, mixed and files on demand', async () => {
    const fetch = vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url === '/api/artifacts?limit=20') {
        return Response.json({ items: listed, next_cursor: null })
      }
      const detail = url.match(/^\/api\/artifacts\/(\d+)\/0$/)
      if (detail) return Response.json(details[Number(detail[1])])
      if (url.endsWith('/5/0/members/0')) return new Response('第一行\n第二行')
      if (url.endsWith('/4/0/members/0')) return new Response('一幅画')
      if (url.endsWith('/4/0/members/1')) {
        return new Response('**夜色**<script>bad()</script>')
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetch)
    const { root, unmount } = mount()
    await settle()

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(root.textContent).toContain('自定义作品')

    const buttons = [...root.querySelectorAll<HTMLButtonElement>('.toggle')]
    buttons[0]!.click()
    buttons[1]!.click()
    buttons[2]!.click()
    buttons[3]!.click()
    buttons[4]!.click()
    await settle()

    expect(root.querySelector('.plain')?.textContent).toContain('第一行\n第二行')
    const mixed = root.querySelectorAll('.work')[1]!
    expect([...mixed.querySelectorAll('h3, .markdown, .images')].map((node) => node.className || node.tagName)).toEqual([
      'H3',
      'markdown',
      'images',
    ])
    expect(mixed.innerHTML).not.toContain('<script')
    expect(mixed.querySelector('img')?.getAttribute('src')).toBe('/api/artifacts/4/0/members/2')
    expect(mixed.querySelector('a')?.getAttribute('href')).toBe('/api/artifacts/4/0/members/2')
    expect(mixed.querySelector('a')?.getAttribute('target')).toBe('_blank')
    expect(root.querySelectorAll('.work')[2]?.querySelector('img')?.src).toContain('/api/artifacts/3/0/members/0')
    expect(root.textContent).toContain('text/html · 30 B · 暂不支持')
    expect(root.textContent).toContain('这件作品暂时无法读取')
    expect(root.innerHTML).not.toContain('data:image')
    unmount()
  })

  it('appends an opaque-cursor page without duplicates and isolates page failure', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ items: [item(3, 'text'), item(2, 'text')], next_cursor: 'opaque/+==' }))
      .mockResolvedValueOnce(Response.json({ items: [item(2, 'text'), item(1, 'text')], next_cursor: 'last' }))
      .mockRejectedValueOnce(new Error('offline'))
    vi.stubGlobal('fetch', fetch)
    const { root } = mount()
    await settle()

    root.querySelector<HTMLButtonElement>('.more')!.click()
    await settle()
    expect(root.querySelectorAll('.work')).toHaveLength(3)
    expect(fetch.mock.calls[1]?.[0]).toBe('/api/artifacts?cursor=opaque%2F%2B%3D%3D&limit=20')

    root.querySelector<HTMLButtonElement>('.more')!.click()
    await settle()
    expect(root.querySelectorAll('.work')).toHaveLength(3)
    expect(root.textContent).toContain('更早的作品暂时没有加载出来')
  })

  it('separates loading, empty and first-page failure states', async () => {
    let resolve!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((done) => { resolve = done })))
    const loadingView = mount()
    const loading = loadingView.root
    expect(loading.textContent).toContain('正在翻')
    resolve(Response.json({ items: [], next_cursor: null }))
    await settle()
    expect(loading.textContent).toContain('还没有已经提交的作品')

    loadingView.unmount()
    document.body.innerHTML = ''
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    const failed = mount().root
    await settle()
    expect(failed.textContent).toContain('作品暂时读不到')
  })

  it('retries only a failed member without refetching its detail', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ items: [item(5, 'text')], next_cursor: null }))
      .mockResolvedValueOnce(Response.json(details[5]))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(new Response('恢复后的正文'))
    vi.stubGlobal('fetch', fetch)
    const { root } = mount()
    await settle()

    const toggle = root.querySelector<HTMLButtonElement>('.toggle')!
    toggle.click()
    await settle()
    expect(root.textContent).toContain('部分内容暂时无法读取')
    toggle.click()
    toggle.click()
    await settle()
    expect(root.textContent).toContain('恢复后的正文')
    expect(fetch).toHaveBeenCalledTimes(4)
  })

  it('can retry when the initial detail request fails', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ items: [item(5, 'text')], next_cursor: null }))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(Response.json(details[5]))
      .mockResolvedValueOnce(new Response('恢复后的正文'))
    vi.stubGlobal('fetch', fetch)
    const { root } = mount()
    await settle()

    const toggle = root.querySelector<HTMLButtonElement>('.toggle')!
    toggle.click()
    await settle()
    expect(root.textContent).toContain('这件作品暂时无法读取')
    toggle.click()
    toggle.click()
    await settle()
    expect(root.textContent).toContain('恢复后的正文')
  })

  it('remounts a failed image on the next expansion', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ items: [item(3, 'image')], next_cursor: null }))
      .mockResolvedValueOnce(Response.json(details[3]))
    vi.stubGlobal('fetch', fetch)
    const { root } = mount()
    await settle()

    const toggle = root.querySelector<HTMLButtonElement>('.toggle')!
    toggle.click()
    await settle()
    root.querySelector('img')!.dispatchEvent(new Event('error'))
    await settle()
    expect(root.querySelector('img')).toBeNull()
    toggle.click()
    toggle.click()
    await settle()
    expect(root.querySelector('img')).not.toBeNull()
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})
