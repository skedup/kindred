import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  artifactMemberUrl,
  fetchArtifactDetail,
  fetchArtifacts,
  fetchArtifactText,
} from './client'

afterEach(() => vi.unstubAllGlobals())

describe('artifact api client', () => {
  it('round-trips the opaque cursor without interpreting it', async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], next_cursor: null }), {
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetch)

    await fetchArtifacts({ cursor: 'host/+opaque==', limit: 7 })

    expect(fetch).toHaveBeenCalledWith(
      '/api/artifacts?cursor=host%2F%2Bopaque%3D%3D&limit=7',
      expect.objectContaining({ headers: { Accept: 'application/json' } }),
    )
  })

  it('uses stable detail and member endpoints', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ members: [] })))
      .mockResolvedValueOnce(new Response('line one\nline two'))
    vi.stubGlobal('fetch', fetch)

    await fetchArtifactDetail(42, 3)
    await expect(fetchArtifactText(42, 3, 8)).resolves.toBe('line one\nline two')

    expect(fetch.mock.calls[0]?.[0]).toBe('/api/artifacts/42/3')
    expect(fetch.mock.calls[1]?.[0]).toBe('/api/artifacts/42/3/members/8')
    expect(artifactMemberUrl(42, 3, 8)).toBe('/api/artifacts/42/3/members/8')
  })
})
