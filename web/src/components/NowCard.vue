<script setup lang="ts">
// 「此刻」卡片 —— 叙事为主，指标可展开。
// companion 看叙事：ta 在做什么、什么心情、在哪。engineer 点开看 7+4+3 维。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { fetchNow } from '../api/client'
import {
  activityPresentation,
  phaseLabel as toPhase,
  weekdayLabel as toWeekday,
} from '../api/labels'
import type { NowResponse } from '../api/types'
import GaugeBar from './GaugeBar.vue'

const data = ref<NowResponse | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const showMetrics = ref(false)
const REFRESH_INTERVAL_MS = 30_000
let refreshTimer: number | null = null

const phaseLabel = computed(() => toPhase(data.value?.narrative.time_phase ?? ''))
const weekdayLabel = computed(() => toWeekday(data.value?.narrative.weekday ?? ''))
const activityView = computed(() =>
  activityPresentation(
    data.value?.narrative.activity_name ?? '',
    data.value?.narrative.activity_step ?? null,
  ),
)
const hasOutfit = computed(() => {
  const o = data.value?.narrative.outfit
  if (!o) return false
  return Boolean(o.top || o.bottom || o.shoes || o.outer || o.accessory.length || o.makeup)
})
// engagement 是 activity 切换决策关键依据（docs/02 §469）——心流投入度，0-1 → 百分比。
const engagementPct = computed(() => {
  const e = data.value?.narrative.engagement
  return typeof e === 'number' ? Math.round(e * 100) : null
})

// 私密层（内衣+袜）。三态 OR：revealed = ?reveal=1 OR 亲密度够（后端算）。
//  - revealed=false：后端根本没返回明文，只有俏皮拒绝语 tease。
//  - revealed=true：有真容，配 hover 遮掩的趣味 mask。
const INTIMATE_MASK: Record<string, string> = {
  bra: '✧ 嘘…',
  panties: '✧ 别看',
  socks: '✧',
}
const intimate = computed(() => data.value?.narrative.intimate ?? null)
const intimateRevealed = computed(() => Boolean(intimate.value?.revealed))
const intimateTease = computed(() => intimate.value?.tease ?? '')
const intimatePieces = computed(() => {
  const i = intimate.value
  if (!i || !i.revealed) return []
  const out: { key: string; value: string; mask: string }[] = []
  for (const key of ['bra', 'panties', 'socks'] as const) {
    const value = i[key]
    if (value) out.push({ key, value, mask: INTIMATE_MASK[key] ?? '✧' })
  }
  return out
})
// 未揭示时：只要有 tease 就显（表示这层存在但被拦）。揭示时：有真容才显。
const hasIntimate = computed(() =>
  intimateRevealed.value ? intimatePieces.value.length > 0 : Boolean(intimateTease.value),
)

function fmtTime(ts: string | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleString('zh-CN', { hour12: false })
}

// 右上角「揭示所有」开关：点开请求 ?reveal=1（揭示开关无条件生效）。
// revealAll = 本次请求是否带 reveal；是否真揭示看响应 intimateRevealed。
// 三态 OR：亲密度够时首次不带 reveal 也会 revealed=true（N-10）——此时开关是 moot。
const revealAll = ref(false)
// 亲密度解锁：未带 reveal 请求却已 revealed → 是 config 亲密度高锁的，开关控不了。
const intimacyUnlocked = computed(() => intimateRevealed.value && !revealAll.value)

async function load(background = false): Promise<void> {
  if (!background) loading.value = true
  error.value = null
  try {
    data.value = await fetchNow({ reveal: revealAll.value })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (!background) loading.value = false
  }
}

async function toggleReveal(): Promise<void> {
  revealAll.value = !revealAll.value
  await load()
}

function refreshWhenVisible(): void {
  if (!document.hidden) void load(true)
}

onMounted(() => {
  void load()
  refreshTimer = window.setInterval(refreshWhenVisible, REFRESH_INTERVAL_MS)
  document.addEventListener('visibilitychange', refreshWhenVisible)
})

onUnmounted(() => {
  if (refreshTimer != null) window.clearInterval(refreshTimer)
  document.removeEventListener('visibilitychange', refreshWhenVisible)
})
</script>

<template>
  <div v-if="loading" class="state-msg">正在看 ta 此刻……</div>
  <div v-else-if="error" class="state-msg error">读不到：{{ error }}</div>
  <div v-else-if="data?.empty" class="state-msg">心还没跑过任何 tick —— ta 的生活还没开始。</div>

  <div v-else-if="data" class="card now">
    <div class="now-head">
      <div class="now-activity">
        <span class="act-name">{{ activityView.heading }}</span>
        <span v-if="data.narrative.location_name" class="act-loc">
          · {{ data.narrative.location_name }}
        </span>
      </div>
      <div class="now-head-right">
        <button
          class="reveal-toggle"
          :class="{ on: intimateRevealed }"
          :disabled="intimacyUnlocked"
          :title="
            intimacyUnlocked
              ? '亲密度已解锁这一层'
              : intimateRevealed
                ? '收起私密层'
                : '揭示所有'
          "
          @click="toggleReveal"
        >
          {{ intimateRevealed ? '◉ 已揭示' : '◌ 揭示所有' }}
        </button>
        <span class="now-ts faint">{{ fmtTime(data.ts) }}</span>
      </div>
    </div>

    <p v-if="data.narrative.activity_desc" class="now-desc">
      {{ activityView.descPrefix }}{{ data.narrative.activity_desc }}
    </p>
    <p v-if="data.narrative.activity_for_what" class="now-forwhat faint">
      — {{ activityView.motivePrefix }}{{ data.narrative.activity_for_what }}
    </p>
    <p v-if="data.narrative.note" class="now-note">「{{ data.narrative.note }}」</p>

    <div class="now-meta">
      <span v-if="data.narrative.mood_note" class="chip">{{ data.narrative.mood_note }}</span>
      <span v-if="data.narrative.activity_step" class="chip faint">
        {{ activityView.stepLabel }}
      </span>
      <!-- with_whom：和谁一起做这件事（语义异于下面物理在场的 others_present，docs/02 §475） -->
      <span v-for="w in data.narrative.with_whom" :key="'ww-' + w" class="chip accent">
        {{ activityView.participantPrefix }} {{ w }}
      </span>
      <span v-if="engagementPct != null" class="chip faint">
        {{ activityView.engagementPrefix }} {{ engagementPct }}%
      </span>
      <span class="chip" :class="data.narrative.user_present ? 'present' : 'faint'">
        {{ data.narrative.user_present ? '你在场' : '你不在' }}
      </span>
      <span v-for="p in data.narrative.others_present" :key="'op-' + p" class="chip faint">
        身边还有 {{ p }}
      </span>
    </div>

    <!-- 环境一条：时间·地点·天气 -->
    <p class="now-env faint">
      <span v-if="phaseLabel">{{ phaseLabel }}</span>
      <span v-if="data.narrative.weekday"> · {{ weekdayLabel }}</span>
      <span v-if="data.narrative.city"> · {{ data.narrative.city }}</span>
      <span v-if="data.narrative.location_type"> · {{ data.narrative.location_type }}</span>
      <span v-if="data.narrative.weather">
        · {{ data.narrative.weather
        }}{{
          data.narrative.temperature != null ? ` ${Math.round(data.narrative.temperature)}°` : ''
        }}</span
      >
    </p>
    <p v-if="data.narrative.ambience" class="now-ambience faint">
      {{ data.narrative.ambience }}
    </p>

    <!-- 穿戴 -->
    <div v-if="hasOutfit" class="section">
      <div class="section-title faint">此刻穿戴</div>
      <div class="outfit">
        <span v-if="data.narrative.outfit.top" class="tag">{{ data.narrative.outfit.top }}</span>
        <span v-if="data.narrative.outfit.bottom" class="tag">{{
          data.narrative.outfit.bottom
        }}</span>
        <span v-if="data.narrative.outfit.outer" class="tag">{{
          data.narrative.outfit.outer
        }}</span>
        <span v-if="data.narrative.outfit.shoes" class="tag">{{
          data.narrative.outfit.shoes
        }}</span>
        <span v-for="a in data.narrative.outfit.accessory" :key="a" class="tag accent">{{ a }}</span>
        <span v-if="data.narrative.outfit.makeup" class="tag"
          >妆：{{ data.narrative.outfit.makeup }}</span
        >
      </div>
    </div>

    <!-- 私密层（内衣+袜）。数据边界（N-6）：
         未揭示 → 只出俏皮拒绝语（明文根本不在响应里）；
         已揭示 → 真容 + hover 遮掩趣味 mask。 -->
    <div v-if="hasIntimate" class="section">
      <div class="section-title faint">贴身的</div>
      <div v-if="intimateRevealed" class="veil-row">
        <span
          v-for="piece in intimatePieces"
          :key="piece.key"
          class="veil tag"
          :title="piece.value"
        >
          <span class="veil-mask">{{ piece.mask }}</span>
          <span class="veil-real">{{ piece.value }}</span>
        </span>
      </div>
      <div v-else class="tease">{{ intimateTease }}</div>
    </div>

    <!-- 心里的念头 -->
    <div v-if="data.narrative.thoughts.length" class="section">
      <div class="section-title faint">心里转着</div>
      <div
        v-for="(t, i) in data.narrative.thoughts"
        :key="i"
        class="thought"
        :class="{ heavy: t.mood_w < 0 }"
      >
        {{ t.description }}
        <span v-if="t.tag" class="thought-tag faint">#{{ t.tag }}</span>
      </div>
    </div>

    <!-- 随身包：hover 才显，平时遮掩“包里藏着点什么” -->
    <div v-if="data.narrative.bag_items.length" class="section">
      <div class="section-title faint">包里</div>
      <div class="veil-row">
        <span v-for="it in data.narrative.bag_items" :key="it" class="veil tag" :title="it">
          <span class="veil-mask">…</span>
          <span class="veil-real">{{ it }}</span>
        </span>
      </div>
    </div>

    <button class="toggle-metrics" @click="showMetrics = !showMetrics">
      {{ showMetrics ? '收起指标 ▴' : '展开指标 ▾' }}
    </button>

    <div v-if="showMetrics" class="metrics">
      <GaugeBar label="body" :gauge="data.metrics.body" />
      <GaugeBar label="mood" :gauge="data.metrics.mood" />
      <GaugeBar label="inner_pulse" :gauge="data.metrics.inner_pulse" />

      <div class="dim-grid">
        <div class="dim-group">
          <div class="dim-title faint">needs</div>
          <div v-for="(v, k) in data.metrics.needs" :key="k" class="dim-row">
            <span class="muted">{{ k }}</span><span class="dim-val">{{ v }}</span>
          </div>
        </div>
        <div class="dim-group">
          <div class="dim-title faint">affect</div>
          <div v-for="(v, k) in data.metrics.affect" :key="k" class="dim-row">
            <span class="muted">{{ k }}</span><span class="dim-val">{{ v }}</span>
          </div>
        </div>
      </div>
      <div v-if="data.metrics.significance != null" class="sig-line faint">
        significance {{ data.metrics.significance }} · trigger {{ data.trigger_source ?? '—' }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.now-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
}
.act-name {
  font-size: 1.15rem;
  font-weight: 600;
}
.act-loc {
  color: var(--text-dim);
}
.now-ts {
  font-size: 0.78rem;
  white-space: nowrap;
}
.now-head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
}
.reveal-toggle {
  font-size: 0.72rem;
  padding: 3px 10px;
  border-radius: 999px;
  cursor: pointer;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  transition:
    background 0.25s ease,
    color 0.25s ease,
    border-color 0.25s ease;
}
.reveal-toggle:disabled {
  cursor: default;
  opacity: 0.7;
}
.reveal-toggle:disabled:hover {
  border-color: var(--gold);
  color: var(--gold);
}
.reveal-toggle:hover {
  border-color: rgba(201, 168, 106, 0.5);
  color: var(--text);
}
.reveal-toggle.on {
  background: rgba(201, 168, 106, 0.16);
  border-color: var(--gold);
  color: var(--gold);
}
.tease {
  font-size: 0.82rem;
  font-style: italic;
  color: var(--accent-soft);
  padding: 4px 2px;
  letter-spacing: 0.02em;
}
.now-desc {
  margin: 12px 0 4px;
  color: var(--text);
}
.now-note {
  margin: 8px 0;
  color: var(--accent-soft);
  font-style: italic;
}
.now-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin: 14px 0 4px;
}
.chip {
  font-size: 0.78rem;
  padding: 3px 11px;
  border-radius: 999px;
  background: var(--bg-card-hover);
  border: 1px solid var(--border);
}
.chip.present {
  color: var(--gold);
  border-color: var(--gold);
}
.now-forwhat {
  margin: 2px 0 0;
  font-size: 0.86rem;
}
.now-env {
  margin: 12px 0 0;
  font-size: 0.82rem;
}
.now-ambience {
  font-size: 0.82rem;
  margin: 4px 0 0;
}
.section {
  margin-top: 18px;
}
.section-title {
  font-size: 0.74rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 8px;
}
.outfit {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.tag {
  font-size: 0.8rem;
  padding: 3px 11px;
  border-radius: 8px;
  background: var(--bg-card-hover);
  border: 1px solid var(--border);
  color: var(--text-dim);
}
.tag.accent {
  color: var(--gold);
  border-color: rgba(201, 168, 106, 0.35);
}
.veil-row {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
/* 私密遮掩：mask 与真容用 grid 叠放在同一格，按钮宽度取两者最宽——
   这样 hover 显真容时不会被裁切，按钮自适应最长内文。 */
.veil {
  display: inline-grid;
  cursor: default;
  background: rgba(178, 86, 104, 0.1);
  border-color: rgba(178, 86, 104, 0.28);
  color: var(--accent-soft);
  white-space: nowrap;
  transition:
    background 0.35s ease,
    color 0.35s ease,
    border-color 0.35s ease;
}
.veil-mask,
.veil-real {
  grid-area: 1 / 1; /* 叠在同一格，格宽 = 两者中更宽者 */
  text-align: center;
  transition:
    opacity 0.3s ease,
    transform 0.3s ease;
}
.veil-mask {
  letter-spacing: 0.04em;
  filter: blur(0.2px);
  opacity: 0.85;
}
.veil-real {
  opacity: 0;
  transform: scale(0.96);
}
.veil:hover {
  background: var(--bg-card-hover);
  border-color: rgba(201, 168, 106, 0.4);
  color: var(--text);
}
.veil:hover .veil-mask {
  opacity: 0;
}
.veil:hover .veil-real {
  opacity: 1;
  transform: scale(1);
  color: var(--gold);
}
.thought {
  font-size: 0.9rem;
  color: var(--text-dim);
  padding: 7px 12px;
  margin-bottom: 6px;
  border-left: 2px solid var(--accent-soft);
  background: rgba(178, 86, 104, 0.06);
  border-radius: 0 8px 8px 0;
}
.thought.heavy {
  border-left-color: var(--text-faint);
  background: rgba(122, 106, 100, 0.08);
}
.thought-tag {
  font-size: 0.74rem;
  margin-left: 6px;
}
.toggle-metrics {
  margin-top: 18px;
  background: transparent;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
  font-size: 0.82rem;
  padding: 0;
}
.toggle-metrics:hover {
  color: var(--text-dim);
}
.metrics {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}
.dim-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  margin-top: 14px;
}
.dim-title {
  font-size: 0.76rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 6px;
}
.dim-row {
  display: flex;
  justify-content: space-between;
  font-size: 0.82rem;
  padding: 2px 0;
}
.dim-val {
  font-variant-numeric: tabular-nums;
}
.sig-line {
  margin-top: 14px;
  font-size: 0.78rem;
}
</style>
