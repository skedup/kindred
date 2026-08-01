// state 枚举字段 → 中文展示文案。
// 抽成独立模块（N-4）：便于单测覆盖，避免 UI 文案靠人工肉眼兜错字。

const PHASE_LABEL: Record<string, string> = {
  morning: '早晨',
  noon: '正午',
  afternoon: '下午',
  evening: '傍晚',
  night: '夜里',
  late_night: '深夜',
}

const WEEKDAY_LABEL: Record<string, string> = {
  Monday: '周一',
  Tuesday: '周二',
  Wednesday: '周三',
  Thursday: '周四',
  Friday: '周五',
  Saturday: '周六',
  Sunday: '周日',
}

/** 时间相位 → 中文；未知值原样返回，空串返回空。 */
export function phaseLabel(phase: string): string {
  if (!phase) return ''
  return PHASE_LABEL[phase] ?? phase
}

/** 星期 → 中文；未知值原样返回，空串返回空。 */
export function weekdayLabel(weekday: string): string {
  if (!weekday) return ''
  return WEEKDAY_LABEL[weekday] ?? weekday
}

interface ActivityPresentation {
  heading: string
  descPrefix: string
  motivePrefix: string
  participantPrefix: string
  engagementPrefix: string
  stepLabel: string | null
}

/** Activity 展示语义；settle 的所有派生文案都属于上一程。 */
export function activityPresentation(name: string, step: string | null): ActivityPresentation {
  const displayName = name || '——'
  const settled = step === 'settle'
  return {
    heading: settled ? `刚结束 · ${displayName}` : displayName,
    descPrefix: settled ? '上一程收尾：' : '',
    motivePrefix: settled ? '上一程是为了：' : '',
    participantPrefix: settled ? '上一程和' : '和',
    engagementPrefix: settled ? '上一程投入' : '投入',
    stepLabel: settled ? '已收尾' : step,
  }
}
