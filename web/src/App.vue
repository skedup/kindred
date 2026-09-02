<script setup lang="ts">
import { ref } from 'vue'
import { fetchEpisodes, fetchStream } from './api/client'
import InteriorHistoryView from './components/InteriorHistoryView.vue'
import NowCard from './components/NowCard.vue'
import ObservabilityView from './components/ObservabilityView.vue'
import TimelineCard from './components/TimelineCard.vue'
import WorksView from './components/WorksView.vue'

type Tab = 'now' | 'interior' | 'stream' | 'episodes' | 'works' | 'observability'
const tab = ref<Tab>('now')
</script>

<template>
  <header class="app-header">
    <h1>Kindred</h1>
    <div class="subtitle">观察 ta 的生活</div>
  </header>

  <nav class="tabs">
    <button class="tab" :class="{ active: tab === 'now' }" @click="tab = 'now'">此刻</button>
    <button class="tab" :class="{ active: tab === 'stream' }" @click="tab = 'stream'">
      生命流
    </button>
    <button class="tab" :class="{ active: tab === 'interior' }" @click="tab = 'interior'">
      内在曲线
    </button>
    <button class="tab" :class="{ active: tab === 'episodes' }" @click="tab = 'episodes'">
      高光
    </button>
    <button class="tab" :class="{ active: tab === 'works' }" @click="tab = 'works'">
      作品
    </button>
    <button
      class="tab"
      :class="{ active: tab === 'observability' }"
      @click="tab = 'observability'"
    >
      消耗
    </button>
  </nav>

  <main>
    <NowCard v-if="tab === 'now'" />
    <InteriorHistoryView v-else-if="tab === 'interior'" />
    <TimelineCard
      v-else-if="tab === 'stream'"
      :key="'stream'"
      :fetcher="fetchStream"
      empty-message="心还没跑过任何 tick —— ta 的生活还没开始。"
    />
    <TimelineCard
      v-else-if="tab === 'episodes'"
      :key="'episodes'"
      :fetcher="fetchEpisodes"
      :highlight="true"
      empty-message="还没有值得记住的高光时刻。"
    />
    <WorksView v-else-if="tab === 'works'" />
    <ObservabilityView v-else />
  </main>
</template>
