import { describe, expect, it } from 'vitest'
import { activityPresentation, phaseLabel, weekdayLabel } from './labels'

describe('phaseLabel', () => {
  it('六个时间相位都有正确中文（防错字回归，N-4）', () => {
    expect(phaseLabel('morning')).toBe('早晨')
    expect(phaseLabel('noon')).toBe('正午')
    expect(phaseLabel('afternoon')).toBe('下午')
    expect(phaseLabel('evening')).toBe('傍晚') // 曾误写「働晚」
    expect(phaseLabel('night')).toBe('夜里')
    expect(phaseLabel('late_night')).toBe('深夜')
  })

  it('空串返回空，未知值原样返回', () => {
    expect(phaseLabel('')).toBe('')
    expect(phaseLabel('unknown_phase')).toBe('unknown_phase')
  })
})

describe('weekdayLabel', () => {
  it('七天都有正确中文', () => {
    expect(weekdayLabel('Monday')).toBe('周一')
    expect(weekdayLabel('Tuesday')).toBe('周二')
    expect(weekdayLabel('Wednesday')).toBe('周三')
    expect(weekdayLabel('Thursday')).toBe('周四')
    expect(weekdayLabel('Friday')).toBe('周五')
    expect(weekdayLabel('Saturday')).toBe('周六')
    expect(weekdayLabel('Sunday')).toBe('周日')
  })

  it('空串返回空，未知值原样返回', () => {
    expect(weekdayLabel('')).toBe('')
    expect(weekdayLabel('Funday')).toBe('Funday')
  })
})

describe('activityPresentation', () => {
  it('活动进行中保持现有语义', () => {
    expect(activityPresentation('take_a_walk', 'walk')).toEqual({
      heading: 'take_a_walk',
      descPrefix: '',
      motivePrefix: '',
      participantPrefix: '和',
      engagementPrefix: '投入',
      stepLabel: 'walk',
    })
  })

  it('settle 的所有 Activity 派生文案都明确属于上一程', () => {
    expect(activityPresentation('take_a_walk', 'settle')).toEqual({
      heading: '刚结束 · take_a_walk',
      descPrefix: '上一程收尾：',
      motivePrefix: '上一程是为了：',
      participantPrefix: '上一程和',
      engagementPrefix: '上一程投入',
      stepLabel: '已收尾',
    })
  })

  it('缺少活动名时仍保持稳定占位', () => {
    expect(activityPresentation('', null).heading).toBe('——')
    expect(activityPresentation('', 'settle').heading).toBe('刚结束 · ——')
  })
})
