<template>
  <section class="page" data-module="dispatch">
    <header class="page-head">
      <div>
        <h2>车辆调度时序图</h2>
        <p class="page-desc">
          驾驶员、任务、桩号、里程是同一版本号下的连续节点；排班台账、应急清单、轨迹详情均由时序回写，三页阶段不再分叉。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="loadPreview">转换前比对</button>
        <button class="btn primary" type="button" :disabled="migrated" @click="runMigration">
          {{ migrated ? '已完成转换' : '执行历史转换' }}
        </button>
      </div>
    </header>

    <!-- 转换前逐车比对 -->
    <div v-if="preview" class="migration-banner" :class="{ conflict: preview.conflict_count > 0 }">
      <div class="banner-head">
        <strong>
          转换前比对：{{ preview.vehicles_checked }} 辆车，
          {{ preview.conflict_count }} 处分页阶段冲突，{{ preview.open_task_count }} 条未结应急任务
        </strong>
        <button class="link" type="button" @click="preview = null">收起</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>车辆编号</th>
            <th>驾驶员</th>
            <th>排班表</th>
            <th>轨迹回放</th>
            <th>应急汇总</th>
            <th>未结任务</th>
            <th>收敛阶段</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="v in preview.per_vehicle" :key="v.vehicle_code" :class="{ 'row-conflict': v.conflict }">
            <td>{{ v.vehicle_code }}</td>
            <td>{{ v.driver || '—' }}</td>
            <td>{{ v.sources['排班表'] ?? '—' }}</td>
            <td>{{ v.sources['轨迹回放'] ?? '—' }}</td>
            <td>{{ v.sources['应急汇总'] ?? '—' }}</td>
            <td>{{ v['未结应急任务'] ? '迁入' : '—' }}</td>
            <td><strong>{{ v['收敛阶段'] }}</strong></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="btn"
        :class="{ primary: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- 时序总览 + 调度操作 -->
    <template v-if="activeTab === 'timeline'">
      <table class="data-table">
        <thead>
          <tr>
            <th>车辆编号</th>
            <th>车牌号</th>
            <th>时序阶段</th>
            <th>占用</th>
            <th>版本号</th>
            <th>占用者 / 任务</th>
            <th>当前里程</th>
            <th>连续节点</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="v in vehicles" :key="v.vehicle_code">
            <td>{{ v.vehicle_code }}</td>
            <td>{{ v['车牌号'] }}</td>
            <td><span class="stage-tag" :class="stageClass(v.stage)">{{ v.stage }}</span></td>
            <td>{{ v.occupied ? '是' : '否' }}</td>
            <td>v{{ v.version }}</td>
            <td>
              <template v-if="v.occupier">
                {{ v.occupier.operator }} / {{ v.occupier.task }}
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td>{{ v.odometer }} km</td>
            <td class="node-chain">
              <span v-for="n in v.timeline" :key="n.seq" class="node-pill" :title="nodeTitle(n)">
                {{ n.type }}
              </span>
              <span v-if="!v.timeline.length" class="muted">无节点</span>
            </td>
            <td class="row-actions">
              <button v-if="!v.occupied" class="link" type="button" @click="openOccupy(v)">占用派车</button>
              <template v-if="v.occupied">
                <button
                  v-if="v.occupier?.operator === '历史迁移'"
                  class="link"
                  type="button"
                  @click="takeover(v)"
                >接管历史任务</button>
                <button
                  v-if="v.stage === '已派车'"
                  class="link"
                  type="button"
                  @click="openAdvance(v, '已出车')"
                >出车(里程)</button>
                <button
                  v-if="v.stage === '已出车'"
                  class="link"
                  type="button"
                  @click="openAdvance(v, '到场作业')"
                >到场(桩号)</button>
                <button class="link" type="button" @click="openRelease(v, false)">收车归库</button>
                <button class="link danger" type="button" @click="openRelease(v, true)">撤销派车</button>
              </template>
            </td>
          </tr>
          <tr v-if="!vehicles.length">
            <td colspan="9" class="empty-state">暂无车辆档案</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 排班台账（时序投影） -->
    <template v-if="activeTab === 'schedule'">
      <p class="projection-note">投影来源：调度时序（阶段由时序回写，只读）</p>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in scheduleCols" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in schedule" :key="row['车辆编号']">
            <td v-for="col in scheduleCols" :key="col">{{ row[col] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 应急清单（时序投影） -->
    <template v-if="activeTab === 'emergency'">
      <p class="projection-note">投影来源：调度时序（仅未结应急任务）</p>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in emergencyCols" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in emergencyRows" :key="row['任务编号']">
            <td v-for="col in emergencyCols" :key="col">{{ row[col] ?? '—' }}</td>
          </tr>
          <tr v-if="!emergencyRows.length">
            <td :colspan="emergencyCols.length" class="empty-state">暂无未结应急任务</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 轨迹详情（时序投影） -->
    <template v-if="activeTab === 'track'">
      <p class="projection-note">投影来源：调度时序（驾驶员→任务→里程→桩号 连续节点回放）</p>
      <div v-for="item in tracks" :key="item['车辆编号']" class="track-card">
        <h4>
          {{ item['车辆编号'] }} ·
          <span class="stage-tag" :class="stageClass(item['轨迹阶段'])">{{ item['轨迹阶段'] }}</span>
          · v{{ item.version }} · {{ item['当前里程'] }} km
        </h4>
        <ol class="track-list">
          <li v-for="n in item.track" :key="n.seq" class="track-node">
            <span class="node-pill">{{ n.type }}</span>
            <span class="muted">#{{ n.seq }} {{ n.at }} → {{ n.stage_after }}</span>
            <span v-if="n.驾驶员">驾驶员：{{ n.驾驶员 }}</span>
            <span v-if="n.任务">任务：{{ n.任务 }}</span>
            <span v-if="n.桩号">桩号：{{ n.桩号 }}</span>
            <span v-if="n.里程 !== undefined && n.里程 !== null">里程：{{ n.里程 }} km</span>
            <span v-if="n.动作" class="muted">（{{ n.动作 }}）</span>
          </li>
          <li v-if="!item.track.length" class="muted">暂无轨迹节点</li>
        </ol>
      </div>
    </template>

    <!-- 历史出勤快照 -->
    <template v-if="activeTab === 'history'">
      <p class="projection-note">历史出勤按原驾驶员与车辆快照保留，只读，不再参与占用。</p>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in historyCols" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in historyRows" :key="row.vehicle_code">
            <td>{{ row.vehicle_code }}</td>
            <td>{{ row.driver_snapshot }}</td>
            <td>{{ row.vehicle_snapshot['车牌号'] }}</td>
            <td>{{ row['出勤日期'] }}</td>
            <td>{{ row['任务'] }}</td>
            <td>{{ row['桩号'] }}</td>
            <td :class="{ 'error-text': row.conflict }">
              {{ row['排班阶段'] }} / {{ row['轨迹阶段'] }} / {{ row['应急阶段'] ?? '—' }}
            </td>
          </tr>
          <tr v-if="!historyRows.length">
            <td :colspan="historyCols.length" class="empty-state">转换后才有历史出勤快照</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 调度操作弹层 -->
    <div v-if="dialog" class="dialog-mask" @click.self="dialog = null">
      <form class="dialog" @submit.prevent="submitDialog">
        <h3>{{ dialog.title }}</h3>
        <p class="muted">车辆 {{ dialog.vehicle.vehicle_code }} · 依据版本号 v{{ dialog.version }}</p>

        <template v-if="dialog.kind === 'occupy'">
          <label><span>驾驶员 *</span><input v-model="form.driver" required /></label>
          <label><span>任务 *</span><input v-model="form.task" required /></label>
          <label><span>作业桩号</span><input v-model="form.stake" placeholder="如 K12+300" /></label>
          <label class="inline"><input v-model="form.isEmergency" type="checkbox" /> 应急任务</label>
          <label><span>调度员</span><input v-model="form.operator" /></label>
        </template>

        <template v-else-if="dialog.kind === 'advance'">
          <label v-if="dialog.target === '已出车'">
            <span>出车表显里程 (km) *</span>
            <input v-model="form.mileage" type="number" step="0.1" required />
          </label>
          <label v-else>
            <span>到场桩号 *</span>
            <input v-model="form.stake" required placeholder="如 K31+800" />
          </label>
          <label><span>调度员</span><input v-model="form.operator" /></label>
        </template>

        <template v-else>
          <label v-if="!dialog.cancel">
            <span>收车表显里程 (km) *</span>
            <input v-model="form.mileage" type="number" step="0.1" :required="!dialog.cancel" />
          </label>
          <label><span>调度员（须为占用者本人）</span>
            <input v-model="form.operator" :placeholder="dialog.vehicle.occupier?.operator" required />
          </label>
          <p v-if="dialog.cancel" class="error-text">撤销派车将取消未结任务，车辆立即回待命。</p>
        </template>

        <footer class="dialog-foot">
          <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
          <span>
            <button class="btn ghost" type="button" @click="dialog = null">取消</button>
            <button class="btn primary" type="submit">提交（版本 v{{ dialog.version }}）</button>
          </span>
        </footer>
      </form>
    </div>

    <footer class="page-foot">
      <span>写入消息均携带 message_id 去重；并发冲突（409）时请刷新版本号后重试，他人占用不会被释放。</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Stage = string
type Node = {
  seq: number
  type: string
  at: string
  stage_after: string
  驾驶员?: string
  任务?: string
  桩号?: string
  里程?: number
  动作?: string
}
type Vehicle = {
  id: number
  vehicle_code: string
  车牌号: string
  车辆类型: string
  stage: Stage
  occupied: boolean
  version: number
  occupier: { operator: string; task: string; task_id: string; at: string } | null
  odometer: number
  timeline: Node[]
  tasks: Array<Record<string, unknown>>
}
type TrackNode = {
  seq: number
  type: string
  at: string
  stage_after: string
  驾驶员?: string
  任务?: string
  桩号?: string
  里程?: number
  动作?: string
}
type TrackItem = {
  车辆编号: string
  轨迹阶段: string
  version: number
  当前里程: number
  占用: string
  track: TrackNode[]
}
type LedgerRow = {
  车辆编号: string
  车牌号: string
  排班阶段: string
  占用: string
  驾驶员: string
  任务: string
  桩号: string
  version: number
  更新时间: string
}
type EmergencyRow = {
  车辆编号: string
  应急阶段: string
  驾驶员: string
  任务: string
  桩号: string
  预警级别: string
  占用: string
  version: number
  任务编号: string
}
type HistoryRow = {
  vehicle_code: string
  driver_snapshot: string
  vehicle_snapshot: { 车牌号: string }
  出勤日期: string
  任务: string
  桩号: string
  排班阶段: string | null
  轨迹阶段: string | null
  应急阶段: string | null
  conflict: boolean
}

const ENDPOINT = '/api/dispatch'
const tabs = [
  { key: 'timeline', label: '时序总览' },
  { key: 'schedule', label: '排班台账' },
  { key: 'emergency', label: '应急清单' },
  { key: 'track', label: '轨迹详情' },
  { key: 'history', label: '历史出勤' },
] as const

const vehicles = ref<Vehicle[]>([])
const schedule = ref<LedgerRow[]>([])
const emergencyRows = ref<EmergencyRow[]>([])
const tracks = ref<TrackItem[]>([])
const historyRows = ref<HistoryRow[]>([])
const preview = ref<Record<string, any> | null>(null)
const migrated = ref(false)
const activeTab = ref<(typeof tabs)[number]['key']>('timeline')
const errorMessage = ref('')

const scheduleCols = ['车辆编号', '车牌号', '排班阶段', '占用', '驾驶员', '任务', '桩号', 'version', '更新时间'] as const
const emergencyCols = ['车辆编号', '应急阶段', '驾驶员', '任务', '桩号', '预警级别', '占用', 'version'] as const
const historyCols = ['车辆编号', '原驾驶员', '车牌号(快照)', '出勤日期', '任务', '桩号', '旧三页阶段(排班/轨迹/应急)'] as const

const stats = computed(() => [
  { label: '在册车辆', value: vehicles.value.length },
  { label: '占用中', value: vehicles.value.filter((v: Vehicle) => v.occupied).length },
  { label: '未结应急', value: emergencyRows.value.length },
  { label: '维修中', value: vehicles.value.filter((v: Vehicle) => v.stage === '维修').length },
])

type Dialog = {
  kind: 'occupy' | 'advance' | 'release'
  title: string
  vehicle: Vehicle
  version: number
  target?: string
  cancel?: boolean
}
const dialog = ref<Dialog | null>(null)
const form = reactive({
  driver: '',
  task: '',
  stake: '',
  operator: '值班管理员',
  mileage: '',
  isEmergency: false,
})

function stageClass(stage: string) {
  return {
    occupied: ['已派车', '已出车', '到场作业'].includes(stage),
    standby: stage === '待命',
    repair: stage === '维修',
  }
}

function nodeTitle(n: Node) {
  return [n.at, n.动作, n.驾驶员, n.任务, n.桩号, n.里程 !== undefined ? `${n.里程}km` : '']
    .filter(Boolean)
    .join(' ')
}

function newMessageId() {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID().replace(/-/g, '')
  }
  return `m${Date.now()}${Math.random().toString(16).slice(2)}`
}

async function getJson(path: string) {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return response.json()
}

async function reload() {
  errorMessage.value = ''
  try {
    const [vehicleData, ledgerData, emergencyData, trackData] = await Promise.all([
      getJson(`${ENDPOINT}/vehicles`),
      getJson(`${ENDPOINT}/schedule-ledger`),
      getJson(`${ENDPOINT}/emergency`),
      getJson(`${ENDPOINT}/tracks`),
    ])
    vehicles.value = vehicleData.items ?? []
    schedule.value = ledgerData.items ?? []
    emergencyRows.value = emergencyData.items ?? []
    tracks.value = trackData.items ?? []
    migrated.value = Boolean(ledgerData.migrated)
    await loadHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度数据读取失败'
  }
}

async function loadHistory() {
  try {
    const data = await getJson(`${ENDPOINT}/migration/history`)
    historyRows.value = data.items ?? []
  } catch {
    historyRows.value = []
  }
}

async function loadPreview() {
  errorMessage.value = ''
  try {
    preview.value = await getJson(`${ENDPOINT}/migration/preview`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '转换前比对失败'
  }
}

async function runMigration() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/migration/run`, {
      method: 'POST',
      body: JSON.stringify({ values: { message_id: newMessageId() } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '转换失败')
    preview.value = null
    await loadPreview()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '转换失败'
  }
}

async function switchTab(key: (typeof tabs)[number]['key']) {
  activeTab.value = key
  if (key === 'history') await loadHistory()
}

async function takeover(vehicle: Vehicle) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/takeover`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          vehicle_code: vehicle.vehicle_code,
          version: vehicle.version,
          message_id: newMessageId(),
          operator: form.operator || '值班管理员',
        },
      }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || '接管未生效，请刷新版本号后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '接管失败'
  }
}

function resetForm() {
  form.driver = ''
  form.task = ''
  form.stake = ''
  form.mileage = ''
  form.isEmergency = false
  form.operator = '值班管理员'
}

function openOccupy(vehicle: Vehicle) {
  resetForm()
  dialog.value = { kind: 'occupy', title: `占用车辆 ${vehicle.vehicle_code}`, vehicle, version: vehicle.version }
}

function openAdvance(vehicle: Vehicle, target: string) {
  resetForm()
  form.mileage = target === '已出车' ? String(vehicle.odometer) : ''
  dialog.value = { kind: 'advance', title: `推进节点：${target}`, vehicle, version: vehicle.version, target }
}

function openRelease(vehicle: Vehicle, cancel: boolean) {
  resetForm()
  dialog.value = {
    kind: 'release',
    title: cancel ? '撤销派车' : '收车归库',
    vehicle,
    version: vehicle.version,
    cancel,
  }
}

async function submitDialog() {
  if (!dialog.value) return
  const d = dialog.value
  errorMessage.value = ''
  const values: Record<string, unknown> = {
    vehicle_code: d.vehicle.vehicle_code,
    version: d.version,
    message_id: newMessageId(),
    operator: form.operator,
  }
  let path = ''
  if (d.kind === 'occupy') {
    path = '/occupy'
    Object.assign(values, {
      driver: form.driver,
      task: form.task,
      stake: form.stake,
      is_emergency: form.isEmergency,
    })
  } else if (d.kind === 'advance') {
    path = '/advance'
    Object.assign(values, { target_stage: d.target })
    if (d.target === '已出车') values.mileage = Number(form.mileage)
    else values.stake = form.stake
  } else {
    path = '/release'
    values.cancel = Boolean(d.cancel)
    if (!d.cancel) values.mileage = Number(form.mileage)
  }

  try {
    const response = await request(`${ENDPOINT}${path}`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      // 409：版本已被他人推进或占用者不匹配——绝不重试释放他人占用
      throw new Error(payload.message || `操作未生效（${response.status}），请刷新版本号后重试`)
    }
    dialog.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度操作未生效'
  }
}

onMounted(reload)
</script>

<style scoped>
.migration-banner {
  background: #fff;
  border: 1px solid var(--border);
  border-left: 4px solid var(--brand);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.migration-banner.conflict { border-left-color: #b54708; }
.banner-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.row-conflict { background: #fffaeb; }
.tab-bar { display: flex; gap: 8px; margin: 4px 0 12px; flex-wrap: wrap; }
.muted { color: var(--muted); }
.stage-tag { padding: 1px 8px; border-radius: 10px; font-size: 12px; border: 1px solid var(--border); }
.stage-tag.occupied { background: #ecfdf3; border-color: #12b76a; color: #027a48; }
.stage-tag.standby { background: #f2f4f7; color: #475467; }
.stage-tag.repair { background: #fef3f2; border-color: #d92d20; color: #b42318; }
.node-chain { display: flex; flex-wrap: wrap; gap: 4px; }
.node-pill {
  display: inline-block;
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 9px;
  background: #eff8ff;
  border: 1px solid #84caff;
  color: #175cd3;
  white-space: nowrap;
}
.projection-note { font-size: 12px; color: var(--muted); margin: 4px 0 8px; }
.track-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin-bottom: 10px; }
.track-card h4 { margin: 0 0 8px; font-size: 14px; }
.track-list { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; }
.track-node { font-size: 13px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.link.danger { color: #b42318; }
.dialog-mask {
  position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 20;
}
.dialog {
  background: #fff; border-radius: 10px; padding: 18px 20px;
  width: 420px; max-width: 92vw; display: flex; flex-direction: column; gap: 10px;
}
.dialog h3 { margin: 0; }
.dialog label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 4px; }
.dialog label.inline { flex-direction: row; align-items: center; gap: 6px; color: #344054; }
.dialog input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.dialog-foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 4px; }
</style>
