import type { InteriorHistoryPoint } from '../api/types'

export type InteriorMetricGroup = 'gauges' | 'needs' | 'affect'

export interface InteriorSeries {
  key: string
  label: string
  color: string
  group: InteriorMetricGroup
}

export function metricValue(point: InteriorHistoryPoint, series: InteriorSeries): number | null {
  if (series.group === 'gauges') {
    const value = point.values[series.key as 'body' | 'mood' | 'inner_pulse']
    return typeof value === 'number' ? value : null
  }
  const value = point.values[series.group][series.key]
  return typeof value === 'number' ? value : null
}

export function pointX(points: InteriorHistoryPoint[], index: number): number {
  if (points.length <= 1) return 0.5
  const first = points[0]?.id ?? 0
  const last = points[points.length - 1]?.id ?? first
  if (first === last) return index / (points.length - 1)
  return ((points[index]?.id ?? first) - first) / (last - first)
}

export function linePath(
  points: InteriorHistoryPoint[],
  series: InteriorSeries,
  width: number,
  height: number,
): string {
  let path = ''
  let drawing = false
  points.forEach((point, index) => {
    const value = metricValue(point, series)
    if (value == null) {
      drawing = false
      return
    }
    const x = pointX(points, index) * width
    const y = height - (Math.max(0, Math.min(100, value)) / 100) * height
    path += `${drawing ? ' L' : 'M'} ${x.toFixed(2)} ${y.toFixed(2)}`
    drawing = true
  })
  return path
}

export function nearestPointIndex(points: InteriorHistoryPoint[], ratio: number): number {
  if (!points.length) return -1
  const target = Math.max(0, Math.min(1, ratio))
  let nearest = 0
  let distance = Number.POSITIVE_INFINITY
  points.forEach((_point, index) => {
    const nextDistance = Math.abs(pointX(points, index) - target)
    if (nextDistance < distance) {
      nearest = index
      distance = nextDistance
    }
  })
  return nearest
}
