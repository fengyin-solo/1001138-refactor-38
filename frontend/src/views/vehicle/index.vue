<template>
  <section class="page" data-module="vehicle">
    <header class="page-head">
      <div>
        <h2>养护车辆管理</h2>
        <p class="page-desc">
          维护车辆档案；调度阶段、占用、版本号由「调度时序图」回写，派车出车与收车归库统一在时序页凭版本号操作。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn primary" to="/dispatch">打开调度时序图</RouterLink>
        <button class="btn" type="button" @click="exportRows">导出养护车辆清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <RouterLink class="link" to="/dispatch">调度时序</RouterLink>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无养护车辆数据，可先登记养护车辆</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条养护车辆记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/vehicle'
const columns = ["车辆编号", "车辆类型", "车牌号", "所属单位", "年检日期", "调度阶段", "是否占用", "占用任务", "当前里程", "调度版本"]
// 派车出车/收车归库已迁至调度时序，这里仅保留档案级动作
const actions = ["送修车辆", "维修完成"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ["车辆编号", "车牌号"]

const stats = computed(() => [
  { label: "在册车辆", value: total.value },
  { label: "占用中", value: rows.value.filter((r) => r['是否占用'] === '是').length },
  { label: "维修中", value: rows.value.filter((r) => String(r.status) === '维修').length },
  { label: "待迁移", value: rows.value.filter((r) => r['调度阶段'] === '待迁移').length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || '养护车辆动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护车辆操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('养护车辆列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护车辆列表读取失败'
  }
}

onMounted(reload)
</script>
