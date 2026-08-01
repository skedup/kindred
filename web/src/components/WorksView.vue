<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchArtifacts } from '../api/client'
import type { ArtifactListItem } from '../api/types'
import { appendArtifactPage } from '../lib/artifacts'
import ArtifactWork from './ArtifactWork.vue'

const items = ref<ArtifactListItem[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref(false)
const pageError = ref(false)

async function loadFirst(): Promise<void> {
  loading.value = true
  error.value = false
  try {
    const response = await fetchArtifacts({ limit: 20 })
    items.value = response.items
    nextCursor.value = response.next_cursor
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}

async function loadMore(): Promise<void> {
  if (!nextCursor.value || loadingMore.value) return
  loadingMore.value = true
  pageError.value = false
  try {
    const response = await fetchArtifacts({ cursor: nextCursor.value, limit: 20 })
    items.value = appendArtifactPage(items.value, response.items)
    nextCursor.value = response.next_cursor
  } catch {
    pageError.value = true
  } finally {
    loadingMore.value = false
  }
}

onMounted(loadFirst)
</script>

<template>
  <div v-if="loading" class="state-msg">正在翻 ta 留下的作品……</div>
  <div v-else-if="error" class="state-msg error">
    作品暂时读不到。<button class="text-action" @click="loadFirst">重试</button>
  </div>
  <div v-else-if="!items.length" class="state-msg">还没有已经提交的作品。</div>
  <section v-else class="works" aria-label="作品">
    <ArtifactWork
      v-for="item in items"
      :key="`${item.tick_id}:${item.artifact_ordinal}`"
      :item="item"
    />
    <p v-if="pageError" class="state-msg error">更早的作品暂时没有加载出来。</p>
    <button class="more" :disabled="nextCursor == null || loadingMore" @click="loadMore">
      {{ nextCursor == null ? '到头了' : loadingMore ? '加载中……' : '加载更早作品' }}
    </button>
  </section>
</template>

<style scoped>
.works { display: grid; gap: 16px; }
.text-action {
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-decoration: underline;
}
</style>
