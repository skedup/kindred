// @vitest-environment jsdom

import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { NowResponse, RelationshipView } from '../api/types'
import NowCard from './NowCard.vue'

function response(thought: string | null): NowResponse {
  return {
    ts: '2026-07-31T16:46:55+08:00',
    trigger_source: 'heartbeat',
    empty: false,
    narrative: {
      activity_name: '',
      activity_desc: '',
      activity_for_what: '',
      activity_step: null,
      engagement: 0,
      with_whom: [],
      location_name: '',
      location_type: null,
      mood_note: '',
      note: null,
      thoughts: thought == null ? [] : [{ description: thought, tag: 'relation', mood_w: 1 }],
      outfit: { top: '', bottom: '', outer: null, shoes: '', accessory: [], makeup: null },
      intimate: { revealed: false, tease: '', bra: null, panties: null, socks: null },
      bag_items: [],
      time_phase: '',
      weekday: '',
      user_present: false,
      others_present: [],
      city: '',
      weather: '',
      temperature: null,
      ambience: '',
    },
    metrics: {
      body: { value: 50, description: '' },
      mood: { value: 50, description: '' },
      inner_pulse: { value: 50, description: '' },
      needs: {},
      affect: {},
      significance: null,
    },
  }
}

async function settle(): Promise<void> {
  for (let i = 0; i < 4; i += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    json: async () => body,
  } as Response
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('NowCard', () => {
  it('refreshes while visible, refreshes on return, and stops after unmount', async () => {
    vi.useFakeTimers()
    let hidden = false
    vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
    let nowCalls = 0
    const profile: RelationshipView = {
      declared_role: 'friend', trust: 75, attachment: 50, attraction: 25, friction: 100,
    }
    let relationshipCalls = 0
    const fetch = vi.fn(async (input: string | URL | Request) => {
      if (String(input) === '/api/relationship') {
        relationshipCalls += 1
        return relationshipCalls === 1
          ? jsonResponse(profile)
          : new Response('unavailable', { status: 503 })
      }
      nowCalls += 1
      return jsonResponse(response(nowCalls === 1 ? '旧念头' : null))
    })
    vi.stubGlobal('fetch', fetch)

    const root = document.createElement('div')
    document.body.append(root)
    const app = createApp(NowCard)
    app.mount(root)
    await settle()
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(root.textContent).toContain('旧念头')
    expect(root.textContent).toContain('朋友')
    const friction = root.querySelectorAll('circle.radar-point')[3]
    expect(friction?.getAttribute('cx')).toBe('24')
    expect(friction?.textContent).toContain('摩擦 100')

    await vi.advanceTimersByTimeAsync(30_000)
    await settle()
    expect(fetch).toHaveBeenCalledTimes(4)
    expect(root.textContent).not.toContain('旧念头')
    expect(root.textContent).toContain('当前关系暂时读不到')
    expect(root.querySelector('svg[aria-label="当前关系四轴雷达图"]')).toBeNull()

    hidden = true
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetch).toHaveBeenCalledTimes(4)

    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(fetch).toHaveBeenCalledTimes(6)

    app.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetch).toHaveBeenCalledTimes(6)
  })

  it('keeps the life view while relationship lookup fails loudly', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) =>
      String(input) === '/api/relationship'
        ? new Response('unavailable', { status: 503 })
        : jsonResponse(response(null)),
    ))
    const root = document.createElement('div')
    document.body.append(root)
    const app = createApp(NowCard)
    app.mount(root)
    await settle()
    expect(root.textContent).toContain('当前关系暂时读不到')
    expect(root.querySelector('svg[aria-label="当前关系四轴雷达图"]')).toBeNull()
    app.unmount()
  })
})
