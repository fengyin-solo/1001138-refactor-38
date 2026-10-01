<template>
  <section class="page" data-module="dispatch">
    <header class="page-head">
      <div>
        <h2>车辆调度时序图</h2>
        <p class="page-desc">
          以车辆为聚合根，驾驶员、任务、桩号、里程沿时间轴连续推进；阶段一处计算，
          原子回写排班台账、应急清单与轨迹详情。并发调度以版本号裁决，占用令牌保证不会误放他人车辆。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="toggleOccupied">{{ onlyOccupied ? '查看全部车辆' : '只看占用中' }}</button>
        <button class="btn" type="button" @click="reload">刷新时序</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">在册车辆</span>
        <strong class="stat-value">{{ vehicles.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">占用中</span>
        <strong class="stat-value" style="color:#b54708">{{ occupiedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待命车辆</span>
        <strong class="stat-value" style="color:#067647">{{ availableCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">迁移状态</span>
        <strong class="stat-value" :style="{ fontSize: '15px', color: migrated ? '#067647' : '#b42318' }">
          {{ migrated ? '已统一到时序' : '存在流程断点' }}
        </strong>
      </article>
    </div>

    <!-- 转换前逐车比对 -->
    <section v-if="comparison" class="panel">
      <div class="panel-head">
        <h3>转换前逐车比对（排班台账 / 应急清单 / 轨迹回放）</h3>
        <span v-if="!migrated" class="hint">
          发现 {{ comparison.broken }}/{{ comparison.total }} 辆车三处阶段不一致；
          历史出勤将按原驾驶员+车辆快照保留，仅未结任务迁入新时序
        </span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>车辆编号</th><th>原驾驶员</th><th>任务</th><th>排班台账</th>
            <th>应急清单</th><th>轨迹回放</th><th>未结</th><th>比对</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in comparison.rows" :key="r.车辆编号" :class="{ 'row-broken': !r.consistent }">
            <td>{{ r.车辆编号 }}</td>
            <td>{{ r.原驾驶员 ?? '—' }}</td>
            <td>{{ r.任务名称 ?? '—' }}</td>
            <td>{{ r.stages['排班台账'] }}</td>
            <td>{{ r.stages['应急清单'] }}</td>
            <td>{{ r.stages['轨迹回放'] }}</td>
            <td>{{ r.未结 ? '是' : '否' }}</td>
            <td>
              <span :class="r.consistent ? 'tag ok' : 'tag bad'">
                {{ r.consistent ? '一致' : '断点' }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!migrated" class="panel-actions">
        <button class="btn primary" type="button" :disabled="migrating" @click="runMigration">
          {{ migrating ? '迁移中…' : '确认比对结果，执行迁移' }}
        </button>
        <span v-if="migrationMsg" class="hint">{{ migrationMsg }}</span>
      </div>
      <div v-else class="panel-actions">
        <span class="tag ok">迁移已完成，新时序是唯一事实来源</span>
      </div>
    </section>

    <!-- 车辆时序列表 -->
    <section class="panel">
      <div class="panel-head"><h3>车辆时序总览（版本号 / 占用令牌归属）</h3></div>
      <table class="data-table">
        <thead>
          <tr>
            <th>车辆编号</th><th>车牌号</th><th>类型</th><th>当前阶段</th>
            <th>驾驶员</th><th>任务</th><th>桩号</th><th>里程(km)</th>
            <th>版本</th><th>占用</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="v in vehicles" :key="v.id">
            <td>{{ v.车辆编号 }}</td>
            <td>{{ v.车牌号 }}</td>
            <td>{{ v.车辆类型 }}</td>
            <td><span :class="stageTag(v)">{{ v.stage }}</span></td>
            <td>{{ v.驾驶员 ?? '—' }}</td>
            <td>{{ v.任务名称 ?? '—' }}<span v-if="v.任务类别" class="kind-mini">{{ v.任务类别 }}</span></td>
            <td>{{ v.桩号 ?? '—' }}</td>
            <td>{{ v.里程 ?? '—' }}</td>
            <td>v{{ v.version }}</td>
            <td>
              <span v-if="v.occupied" class="tag warn">令牌 {{ (v.占用令牌 ?? '').slice(0, 6) }}</span>
              <span v-else class="tag ok">空闲</span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="openDetail(v)">时序详情</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 单车时序详情 -->
    <section v-if="detail" class="panel detail-panel">
      <div class="panel-head">
        <h3>{{ detail.vehicle.车辆编号 }} · 调度时序（v{{ detail.vehicle.version }}）</h3>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </div>

      <!-- 连续节点流 -->
      <div class="node-flow">
        <template v-for="(n, i) in detail.nodes" :key="n.id">
          <div class="node" :class="{ current: i === detail.nodes.length - 1 }">
            <div class="node-kind">{{ n.kind_label }}</div>
            <div class="node-stage">{{ n.阶段 }}</div>
            <div class="node-meta">
              <div>驾驶员：{{ n.驾驶员 ?? '—' }}</div>
              <div>任务：{{ n.任务名称 ?? '—' }} <span class="kind-mini">{{ n.任务类别 }}</span></div>
              <div>桩号：{{ n.桩号 ?? '—' }}</div>
              <div>里程：{{ n.里程 ?? '—' }} km</div>
              <div class="node-time">{{ n.时间 }}</div>
            </div>
          </div>
          <div v-if="i < detail.nodes.length - 1" class="node-arrow">→</div>
        </template>
      </div>

      <!-- 命令台 -->
      <div class="command-box">
        <h4>命令台（消息去重后应用 · 占用车辆与推进节点原子成功）</h4>
        <div class="cmd-grid">
          <label><span>消息编号 message_id</span>
            <input v-model="cmd.message_id" placeholder="留空则不去重，建议填业务消息号" /></label>
          <label><span>驾驶员</span><input v-model="cmd.driver" :disabled="detail.vehicle.occupied" /></label>
          <label><span>任务名称</span><input v-model="cmd.task_name" :disabled="detail.vehicle.occupied" /></label>
          <label><span>任务类别</span>
            <select v-model="cmd.task_kind" :disabled="detail.vehicle.occupied">
              <option value="排班">排班</option><option value="应急">应急</option>
            </select></label>
          <label><span>桩号</span><input v-model="cmd.stake" placeholder="如 K12+300" /></label>
          <label><span>里程(km)</span><input v-model.number="cmd.mileage" type="number" /></label>
          <label v-if="detail.vehicle.occupied"><span>占用令牌</span>
            <input v-model="cmd.token" placeholder="推进/收车必须持本人令牌" /></label>
        </div>
        <div class="cmd-actions">
          <button v-if="!detail.vehicle.occupied && detail.vehicle.stage !== '维修中'"
                  class="btn primary" type="button" @click="sendDispatch">
            派车占用（携带 v{{ detail.vehicle.version }}）
          </button>
          <template v-if="detail.vehicle.occupied">
            <button v-for="s in nextStages" :key="s" class="btn" type="button" @click="advanceTo(s)">
              推进 → {{ s }}
            </button>
            <button class="btn" type="button" @click="closeTask('收车')">收车归库</button>
            <button class="btn" type="button" @click="closeTask('送修')">送修车辆</button>
          </template>
          <button v-if="detail.vehicle.stage === '维修中'" class="btn primary" type="button" @click="repairDone">
            修复完成 → 在库待命
          </button>
        </div>
        <p v-if="cmdResult" class="hint" :class="cmdResult.ok ? 'ok-text' : 'error-text'">{{ cmdResult.message }}</p>
      </div>

      <!-- 三处回写 -->
      <div class="writeback">
        <h4>阶段回写（同一时序投影）</h4>
        <div class="wb-cols">
          <div>
            <h5>排班台账</h5>
            <pre>{{ JSON.stringify(detail.writeback.schedule ?? { '提示': '无在途排班' }, null, 2) }}</pre>
          </div>
          <div>
            <h5>应急清单</h5>
            <pre>{{ JSON.stringify(detail.writeback.emergency ?? { '提示': '非应急任务' }, null, 2) }}</pre>
          </div>
          <div>
            <h5>轨迹详情（最新节点）</h5>
            <pre>{{ JSON.stringify(detail.writeback.track ?? { '提示': '暂无轨迹' }, null, 2) }}</pre>
          </div>
        </div>
      </div>
    </section>

    <footer class="page-foot">
      <span>时序阶段为唯一事实来源 · 历史出勤快照 {{ attendanceCount }} 条 · 消息日志 {{ messageCount }} 条</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  dispatchApi,
  type ComparisonRow,
  type TimelineNode,
  type TimelineVehicle,
  STAGE_SEQUENCE,
} from '@/api/dispatch'

const vehicles = ref<TimelineVehicle[]>([])
const migrated = ref(false)
const onlyOccupied = ref(false)
const comparison = ref<{ migrated: boolean; total: number; broken: number; rows: ComparisonRow[] } | null>(null)
const detail = ref<{ vehicle: TimelineVehicle; nodes: TimelineNode[]; writeback: Record<string, unknown> } | null>(null)
const migrating = ref(false)
const migrationMsg = ref('')
const errorMessage = ref('')
const cmdResult = ref<{ ok: boolean; message: string } | null>(null)
const attendanceCount = ref(0)
const messageCount = ref(0)

const cmd = reactive({
  message_id: '',
  driver: '',
  task_name: '',
  task_kind: '排班',
  stake: '',
  mileage: null as number | null,
  token: '',
})

const occupiedCount = computed(() => vehicles.value.filter((v) => v.occupied).length)
const availableCount = computed(() => vehicles.value.filter((v) => !v.occupied).length)
const nextStages = computed(() => {
  if (!detail.value) return []
  const idx = detail.value.vehicle.stage_index
  // 节点只能逐格前进：只提示当前阶段的下一格
  return STAGE_SEQUENCE.slice(idx + 1, idx + 2)
})

function stageTag(v: TimelineVehicle): string {
  if (v.stage === '在库待命') return 'tag ok'
  if (v.stage === '维修中') return 'tag bad'
  if (v.stage === '收车归库') return 'tag idle'
  return 'tag warn'
}

async function reload() {
  errorMessage.value = ''
  try {
    const [tl, cmp, att, msgs] = await Promise.all([
      dispatchApi.listTimelines(onlyOccupied.value),
      dispatchApi.comparison(),
      dispatchApi.attendance(),
      dispatchApi.messages(),
    ])
    vehicles.value = tl.items
    migrated.value = tl.migrated
    comparison.value = cmp
    attendanceCount.value = att.items.length
    messageCount.value = msgs.items.length
    if (detail.value) await openDetailById(detail.value.vehicle.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '时序加载失败'
  }
}

function toggleOccupied() {
  onlyOccupied.value = !onlyOccupied.value
  void reload()
}

async function runMigration() {
  migrating.value = true
  migrationMsg.value = ''
  try {
    const res = await dispatchApi.migrate()
    migrationMsg.value = res.message
    await reload()
  } finally {
    migrating.value = false
  }
}

async function openDetail(v: TimelineVehicle) {
  await openDetailById(v.id)
  cmd.message_id = ''
  cmd.driver = v.驾驶员 ?? ''
  cmd.task_name = v.任务名称 ?? ''
  cmd.task_kind = v.任务类别 ?? '排班'
  cmd.stake = v.桩号 ?? ''
  cmd.mileage = v.里程 ?? null
  cmd.token = ''
  cmdResult.value = null
}

async function openDetailById(id: number) {
  detail.value = await dispatchApi.timeline(id)
}

function baseCommand(): Record<string, unknown> {
  if (!detail.value) return {}
  const v = detail.value.vehicle
  const body: Record<string, unknown> = {
    vehicle_id: v.id,
    expected_version: v.version,
    message_id: cmd.message_id || null,
  }
  if (v.occupied) body.token = cmd.token || null
  return body
}

async function send(command: string, extra: Record<string, unknown>) {
  errorMessage.value = ''
  cmdResult.value = null
  const res = await dispatchApi.command({ command, ...baseCommand(), ...extra })
  cmdResult.value = { ok: res.ok, message: res.duplicate ? `消息去重：${res.message}` : res.message }
  if (!res.ok && !res.duplicate) {
    errorMessage.value = res.message
  }
  await reload()
}

function sendDispatch() {
  return send('dispatch', {
    driver: cmd.driver,
    task_name: cmd.task_name,
    task_kind: cmd.task_kind,
    stake: cmd.stake,
    mileage: cmd.mileage,
  })
}

// 模板里 @click 绑定
function advanceTo(stage: string) {
  return send('advance', { stage, stake: cmd.stake || null, mileage: cmd.mileage ?? null })
}
function closeTask(closing: string) {
  return send('advance', { closing, stake: cmd.stake || null, mileage: cmd.mileage ?? null })
}
function repairDone() {
  return send('repair_done', { message_id: cmd.message_id || null })
}
</script>

<style scoped>
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 8px;
}
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-actions { margin-top: 10px; display: flex; align-items: center; gap: 10px; }
.hint { color: var(--muted); font-size: 12px; }
.ok-text { color: #067647; }
.row-broken { background: #fef3f2; }
.tag {
  display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 12px; white-space: nowrap;
}
.tag.ok { background: #e7f6ec; color: #067647; }
.tag.warn { background: #fef4e5; color: #b54708; }
.tag.bad { background: #fdeaea; color: #b42318; }
.tag.idle { background: #eef2f6; color: #475569; }
.kind-mini {
  margin-left: 6px; font-size: 11px; color: #1f6feb;
  border: 1px solid #bcd6fb; border-radius: 8px; padding: 0 6px;
}
.node-flow {
  display: flex;
  align-items: stretch;
  gap: 6px;
  overflow-x: auto;
  padding-bottom: 8px;
}
.node {
  min-width: 190px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  background: #fbfdff;
}
.node.current { border-color: var(--brand); box-shadow: 0 0 0 2px rgba(31, 111, 235, 0.15); }
.node-kind { font-size: 12px; color: var(--muted); }
.node-stage { font-weight: 600; margin: 2px 0 6px; }
.node-meta { font-size: 12px; line-height: 1.7; color: #334155; }
.node-time { color: var(--muted); font-size: 11px; }
.node-arrow { align-self: center; color: var(--brand); font-weight: 700; }
.command-box {
  border-top: 1px dashed var(--border);
  margin-top: 12px;
  padding-top: 10px;
}
.command-box h4 { margin: 0 0 8px; font-size: 14px; }
.cmd-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(180px, 1fr));
  gap: 8px 12px;
}
.cmd-grid label span { display: block; font-size: 12px; color: var(--muted); }
.cmd-grid input, .cmd-grid select {
  width: 100%; padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px;
}
.cmd-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
.writeback { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 10px; }
.writeback h4 { margin: 0 0 8px; font-size: 14px; }
.wb-cols { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.wb-cols h5 { margin: 0 0 4px; font-size: 12px; color: var(--muted); }
.wb-cols pre {
  background: #0f172a; color: #d7e3f4; border-radius: 6px;
  padding: 8px; font-size: 11px; line-height: 1.5;
  max-height: 220px; overflow: auto; margin: 0;
}
.detail-panel { border-left: 4px solid var(--brand); }
</style>
