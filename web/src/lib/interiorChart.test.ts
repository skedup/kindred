import { describe, expect, it } from 'vitest'
import type { InteriorHistoryPoint } from '../api/types'
import {
  linePath,
  metricValue,
  nearestPointIndex,
  pointX,
  type InteriorSeries,
} from './interiorChart'

const series: InteriorSeries = {
  key: 'mood',
  label: '心情',
  color: '#fff',
  group: 'gauges',
}

function point(id: number, mood: number | null): InteriorHistoryPoint {
  return {
    id,
    ts: null,
    trigger_source: null,
    activity_name: '',
    activity_step: null,
    note: null,
    significance: null,
    values: {
      body: null,
      mood,
      inner_pulse: null,
      needs: {},
      affect: {},
    },
  }
}

describe('interior chart geometry', () => {
  it('uses tick ids for the horizontal position', () => {
    const points = [point(10, 20), point(12, 40), point(20, 60)]
    expect(pointX(points, 0)).toBe(0)
    expect(pointX(points, 1)).toBe(0.2)
    expect(pointX(points, 2)).toBe(1)
    expect(nearestPointIndex(points, 0.24)).toBe(1)
  })

  it('creates a new path segment after a missing value', () => {
    const points = [point(1, 20), point(2, null), point(3, 80)]
    const path = linePath(points, series, 100, 100)
    expect(path).toContain('M 0.00 80.00')
    expect(path).toContain('M 100.00 20.00')
    expect(path).not.toContain(' L')
  })

  it('reads grouped values without coercing missing fields', () => {
    const needs: InteriorSeries = {
      key: 'energy',
      label: '精力',
      color: '#fff',
      group: 'needs',
    }
    const item = point(1, 55)
    item.values.needs.energy = 72
    expect(metricValue(item, series)).toBe(55)
    expect(metricValue(item, needs)).toBe(72)
  })
})
