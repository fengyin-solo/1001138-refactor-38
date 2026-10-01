/** 车辆调度时序图 API 封装。 */
import { request } from './client'

const BASE = '/api/dispatch'

export interface TimelineVehicle {
  id: number
  车辆编号: string
  车牌号: string
  车辆类型: string
  所属单位: string
  version: number
  stage: string
  stage_index: number
  occupied: boolean
  占用令牌: string | null
  驾驶员: string | null
  任务名称: string | null
  任务类别: string | null
  桩号: string | null
  里程: number | null
  更新时间: string | null
  节点数: number
  applied_node_id?: number
}

export interface TimelineNode {
  id: number
  kind: string
  kind_label: string
  阶段: string
  阶段序号: number
  驾驶员: string | null
  任务名称: string | null
  任务类别: string | null
  桩号: string | null
  里程: number | null
  时间: string
  message_id: string
}

export interface CommandResult {
  ok: boolean
  duplicate?: boolean
  message: string
  message_log?: { id: number; message_id: string; command: string; status: string; detail: string; time: string }
  result?: TimelineVehicle
}

export interface ComparisonRow {
  车辆id: number
  车辆编号: string
  车牌号: string
  原驾驶员: string | null
  任务名称: string | null
  任务类别: string | null
  未结: boolean
  stages: Record<string, string>
  consistent: boolean
}

async function getJson<T>(path: string): Promise<T> {
  const resp = await request(`${BASE}${path}`)
  if (!resp.ok) throw new Error(`接口返回 ${resp.status}`)
  return (await resp.json()) as T
}

export const dispatchApi = {
  listTimelines: (onlyOccupied = false) =>
    getJson<{ items: TimelineVehicle[]; total: number; migrated: boolean }>(
      `/timelines?only_occupied=${onlyOccupied}`,
    ),
  timeline: (id: number) =>
    getJson<{ vehicle: TimelineVehicle; nodes: TimelineNode[]; writeback: Record<string, unknown> }>(
      `/timelines/${id}`,
    ),
  comparison: () =>
    getJson<{ migrated: boolean; total: number; broken: number; rows: ComparisonRow[] }>(
      '/migration/comparison',
    ),
  migrate: async () => {
    const resp = await request(`${BASE}/migration/run`, { method: 'POST' })
    return (await resp.json()) as { ok: boolean; message: string; migrated_vehicles?: string[] }
  },
  command: async (body: Record<string, unknown>): Promise<CommandResult> => {
    const resp = await request(`${BASE}/commands`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    return (await resp.json()) as CommandResult
  },
  schedule: () => getJson<{ items: Record<string, unknown>[] }>('/schedule'),
  emergency: () => getJson<{ items: Record<string, unknown>[] }>('/emergency'),
  tracks: () => getJson<{ items: Record<string, unknown>[] }>('/tracks'),
  attendance: () => getJson<{ items: Record<string, unknown>[] }>('/attendance'),
  messages: () => getJson<{ items: Record<string, unknown>[] }>('/messages'),
}

export const STAGE_SEQUENCE = ['已派车', '赶赴现场', '现场作业', '应急处置', '巡查归队', '收车归库']
