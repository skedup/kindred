<script setup lang="ts">
// 时间线卡片 —— /stream 和 /episodes 共用（同形状，cursor 分页）。
// 仅 fetcher + 空态文案 + 是否标高光不同，由 props 注入。
import { onMounted, ref } from 'vue'
import type { StreamItem, StreamResponse } from '../api/types'

const props = defineProps<{
  fetcher: (params?: { before?: number; limit?: number }) => Promise<StreamResponse>
  emptyMessage: string
  /** 高光视图：每条左侧点用金色，并显示 significance 数字 */
  highlight?: boolean
}>()

const items = ref<StreamItem[]>([])
const nextCursor = ref<number | null>(null)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref<string | null>(null)
const isEmpty = ref(false)

function fmtTime(ts: string | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleString('zh-CN', { hour12: false })
}

async function loadFirst(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const resp = await props.fetcher({})
    items.value = resp.items
    nextCursor.value = resp.next_cursor
    isEmpty.value = resp.empty
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function loadMore(): Promise<void> {
  if (nextCursor.value == null || loadingMore.value) return
  loadingMore.value = true
  try {
    const resp = await props.fetcher({ before: nextCursor.value })
    items.value = [...items.value, ...resp.items]
    nextCursor.value = resp.next_cursor
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loadingMore.value = false
  }
}

onMounted(loadFirst)
</script>

<template>
  <div v-if="loading" class="state-msg">正在翻 ta 的日子……</div>
  <div v-else-if="error" class="state-msg error">读不到：{{ error }}</div>
  <div v-else-if="isEmpty" class="state-msg">{{ emptyMessage }}</div>

  <div v-else class="card timeline">
    <div
      v-for="item in items"
      :key="item.id"
      class="tl-item"
      :class="{ hl: highlight }"
    >
      <div class="tl-dot" :class="{ gold: highlight }" />
      <div class="tl-body">
        <div class="tl-head">
          <span v-if="item.target_activity" class="tl-act">{{ item.target_activity }}</span>
          <span v-else class="tl-act faint">——</span>
          <span
            v-if="highlight && item.significance != null"
            class="tl-sig"
            >★ {{ item.significance }}</span
          >
          <span class="tl-ts faint">{{ fmtTime(item.ts) }}</span>
        </div>
        <p v-if="item.note" class="tl-note">{{ item.note }}</p>
      </div>
    </div>

    <button class="more" :disabled="nextCursor == null || loadingMore" @click="loadMore">
      {{ nextCursor == null ? '到头了' : loadingMore ? '翻着……' : '看更早的' }}
    </button>
  </div>
</template>

<style scoped>
.tl-item {
  display: flex;
  gap: 14px;
  padding: 14px 0;
  border-bottom: 1px solid var(--border);
}
.tl-item:last-of-type {
  border-bottom: none;
}
.tl-dot {
  flex: none;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent-soft);
  margin-top: 7px;
}
.tl-dot.gold {
  background: var(--gold);
  box-shadow: 0 0 8px rgba(201, 168, 106, 0.5);
}
.tl-body {
  flex: 1;
  min-width: 0;
}
.tl-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.tl-act {
  font-weight: 600;
  font-size: 0.95rem;
}
.tl-sig {
  color: var(--gold);
  font-size: 0.8rem;
}
.tl-ts {
  font-size: 0.76rem;
  margin-left: auto;
  white-space: nowrap;
}
.tl-note {
  margin: 6px 0 0;
  color: var(--text-dim);
  font-size: 0.9rem;
}
</style>
