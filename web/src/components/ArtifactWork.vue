<script setup lang="ts">
import { computed, ref } from 'vue'
import { artifactMemberUrl, fetchArtifactDetail, fetchArtifactText } from '../api/client'
import type { ArtifactDetailResponse, ArtifactListItem, ArtifactMemberRole } from '../api/types'
import {
  artifactAvailabilityLabel,
  artifactKindLabel,
  formatArtifactBytes,
  formatArtifactTime,
  memberAvailabilityLabel,
  renderArtifactMarkdown,
} from '../lib/artifacts'

const props = defineProps<{ item: ArtifactListItem }>()
const opened = ref(false)
const loading = ref(false)
const error = ref(false)
const detail = ref<ArtifactDetailResponse | null>(null)
const texts = ref<Record<number, string>>({})
const failures = ref(new Set<number>())

const members = (...roles: ArtifactMemberRole[]) =>
  detail.value?.members.filter(({ role }) => roles.includes(role)) ?? []
const titles = computed(() => members('title'))
const contents = computed(() => members('content'))
const images = computed(() => members('image'))
const files = computed(() =>
  detail.value?.members.filter(
    ({ role, availability }) => role === 'file' || availability !== 'available',
  ) ?? [],
)

async function toggle(): Promise<void> {
  opened.value = !opened.value
  if (!opened.value || loading.value) return
  loading.value = true
  error.value = false
  failures.value = new Set()
  try {
    detail.value ??= await fetchArtifactDetail(props.item.tick_id, props.item.artifact_ordinal)
    await Promise.all(
      members('title', 'content')
        .filter(
          ({ availability, member_ordinal }) =>
            availability === 'available'
            && !Object.hasOwn(texts.value, member_ordinal),
        )
        .map(async (member) => {
          try {
            texts.value[member.member_ordinal] = await fetchArtifactText(
              props.item.tick_id,
              props.item.artifact_ordinal,
              member.member_ordinal,
            )
          } catch {
            markFailed(member.member_ordinal)
          }
        }),
    )
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}

function markFailed(ordinal: number): void {
  failures.value = new Set([...failures.value, ordinal])
}
</script>

<template>
  <article class="card work">
    <header>
      <div>
        <span class="kind">{{ artifactKindLabel[item.kind] }}</span>
        <h2>{{ item.label }}</h2>
      </div>
      <time>{{ formatArtifactTime(item.ts) }}</time>
    </header>
    <div class="meta">
      <span>{{ item.activity_name }}</span>
      <span>{{ item.member_count }} 项</span>
      <span>{{ formatArtifactBytes(item.total_bytes) }}</span>
      <span :class="item.availability">{{ artifactAvailabilityLabel[item.availability] }}</span>
    </div>
    <button class="toggle" :aria-expanded="opened" @click="toggle">
      {{ opened ? '收起内容 ▴' : '展开内容 ▾' }}
    </button>

    <section v-if="opened" class="content">
      <p v-if="loading" class="faint">正在展开……</p>
      <p v-else-if="error || item.availability === 'unavailable'" class="error">
        这件作品暂时无法读取。
      </p>
      <template v-else-if="detail">
        <h3 v-for="member in titles" :key="member.member_ordinal">
          {{ texts[member.member_ordinal] }}
        </h3>
        <template v-for="member in contents" :key="member.member_ordinal">
          <div
            v-if="member.media_type === 'text/markdown' && texts[member.member_ordinal]"
            class="markdown"
            v-html="renderArtifactMarkdown(texts[member.member_ordinal] ?? '')"
          />
          <p v-else-if="texts[member.member_ordinal]" class="plain">
            {{ texts[member.member_ordinal] }}
          </p>
        </template>
        <div v-if="images.length" class="images">
          <a
            v-for="member in images.filter(
              (entry) => entry.availability === 'available' && !failures.has(entry.member_ordinal),
            )"
            :key="member.member_ordinal"
            :href="artifactMemberUrl(item.tick_id, item.artifact_ordinal, member.member_ordinal)"
            :aria-label="`查看原图：${item.label}`"
            target="_blank"
            rel="noopener noreferrer"
          >
            <img
              :src="artifactMemberUrl(item.tick_id, item.artifact_ordinal, member.member_ordinal)"
              :alt="item.label"
              loading="lazy"
              @error="markFailed(member.member_ordinal)"
            />
          </a>
        </div>
        <ul v-if="files.length" class="files">
          <li v-for="member in files" :key="member.member_ordinal">
            {{ member.media_type }} · {{ formatArtifactBytes(member.bytes) }} ·
            {{ memberAvailabilityLabel[member.availability] }}
          </li>
        </ul>
        <p v-if="failures.size" class="error">部分内容暂时无法读取。</p>
      </template>
    </section>
  </article>
</template>

<style scoped>
.work { min-width: 0; padding: 20px 22px; overflow: hidden; border-radius: 8px; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.kind { color: var(--gold); font-size: 0.72rem; }
h2, h3 { margin: 1px 0 0; overflow-wrap: anywhere; font-size: 1.05rem; }
time, .meta { color: var(--text-faint); font-size: 0.76rem; }
time { flex: none; white-space: nowrap; }
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 9px;
}
.partial { color: var(--gold); }
.unavailable, .error { color: var(--accent-soft); }
.toggle {
  margin-top: 14px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}
.content { margin-top: 16px; padding-top: 16px; overflow-wrap: anywhere; border-top: 1px solid var(--border); }
h3 { margin-bottom: 12px; font-size: 1rem; }
.markdown, .plain { color: var(--text-dim); white-space: pre-wrap; }
.markdown :deep(pre), .markdown :deep(table) { display: block; max-width: 100%; overflow-x: auto; }
.markdown :deep(img), .images img { display: block; max-width: 100%; height: auto; }
.images {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
  gap: 10px;
  margin-top: 14px;
}
.images a { min-width: 0; }
.images img { width: 100%; border-radius: 6px; }
.files { padding-left: 18px; color: var(--text-faint); font-size: 0.78rem; }
@media (max-width: 620px) {
  .work { padding: 17px 16px; }
  header { display: block; }
  time { white-space: normal; }
}
</style>
