"""车辆调度时序领域模型。

重构目标：消除「排班表 / 轨迹回放 / 应急汇总」三处各写各状态造成的流程断点。
车辆只有一条事实来源——调度时序（timeline）：驾驶员、任务、桩号、里程都是
挂在同一版本号下的连续节点；排班台账、应急清单、轨迹详情均为时序的只读投影。

并发口径：
- 占用车辆与推进首节点在同一把锁、同一次提交里完成（原子成功）；
- 所有写消息先做 message_id 去重，再应用；
- 每次成功写入 version += 1，并发调度只认可先取得该版本号的任务（CAS）；
- 版本过期或占用者不匹配时整体失败，且绝不释放他人占用。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "dispatch"

# 时序上的车辆阶段（唯一事实口径）
STAGE_STANDBY = "待命"          # 在库可派
STAGE_ASSIGNED = "已派车"       # 占用成功，驾驶员/任务节点已挂接
STAGE_DEPARTED = "已出车"       # 离场，里程节点
STAGE_ONSITE = "到场作业"       # 桩号节点（应急任务同样停在作业阶段）
STAGE_RETURNED = "已归库"       # 收车里程节点后车辆随即释放回待命
STAGE_REPAIR = "维修"           # 与调度占用互斥的登记状态

STAGES = [STAGE_STANDBY, STAGE_ASSIGNED, STAGE_DEPARTED, STAGE_ONSITE, STAGE_RETURNED, STAGE_REPAIR]

# 旧三页阶段到新时序阶段的映射，仅迁移使用
LEGACY_STAGE_MAP = {
    "已排班": STAGE_ASSIGNED,
    "已派车": STAGE_ASSIGNED,
    "派车中": STAGE_ASSIGNED,
    "已出车": STAGE_DEPARTED,
    "行驶中": STAGE_DEPARTED,
    "到场作业": STAGE_ONSITE,
    "作业中": STAGE_ONSITE,
    "已归库": STAGE_STANDBY,
    "在库": STAGE_STANDBY,
    "待命": STAGE_STANDBY,
}

# 节点类型：驾驶员 / 任务 / 桩号 / 里程 四类连续节点
NODE_DRIVER = "驾驶员"
NODE_TASK = "任务"
NODE_STAKE = "桩号"
NODE_MILEAGE = "里程"

TASK_OPEN = "未结"
TASK_DONE = "已完成"
TASK_CANCELLED = "已取消"

# 占用型阶段：处于这些阶段的车辆已被某个任务持有
OCCUPYING_STAGES = {STAGE_ASSIGNED, STAGE_DEPARTED, STAGE_ONSITE}


def now_ts() -> str:
    """统一用本地可读时间戳；内存实现不做跨时区持久化。"""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def new_message_id() -> str:
    return uuid.uuid4().hex


class ConflictError(Exception):
    """版本冲突 / 占用者不匹配等并发错误。"""


# ---------------------------------------------------------------------------
# 迁移前的旧口径数据（排班表 / 轨迹回放 / 应急汇总）
#
# 旧系统三页各存一份车辆阶段，同一辆车可能互相对不上——这正是要修的流程断点。
# 迁移时逐车比对这三份快照，冲突的先报告再按统一规则收敛进时序。
# ---------------------------------------------------------------------------
LEGACY_SCHEDULE = [
    {"车辆编号": "VEHI-0001", "阶段": "已排班", "驾驶员": "王建国",
     "任务": "K12+300 路面坑槽修补", "桩号": "K12+300",
     "更新时间": "2026-09-30 07:40", "出勤日期": "2026-09-30"},
    {"车辆编号": "VEHI-0002", "阶段": "已出车", "驾驶员": "李海燕",
     "任务": "K31+800 防汛排水应急", "桩号": "K31+800",
     "更新时间": "2026-09-30 09:05", "出勤日期": "2026-09-30"},
    {"车辆编号": "VEHI-0003", "阶段": "已归库", "驾驶员": "赵德柱",
     "任务": "S2 标绿化修剪", "桩号": "K8+050",
     "更新时间": "2026-09-29 16:20", "出勤日期": "2026-09-29"},
]

LEGACY_TRACKS = [
    {"车辆编号": "VEHI-0001", "阶段": "行驶中", "驾驶员": "王建国",
     "桩号": "K11+900", "里程": 128420,
     "更新时间": "2026-09-30 08:12", "出勤日期": "2026-09-30"},
    {"车辆编号": "VEHI-0002", "阶段": "作业中", "驾驶员": "李海燕",
     "桩号": "K31+800", "里程": 205110,
     "更新时间": "2026-09-30 09:48", "出勤日期": "2026-09-30"},
    {"车辆编号": "VEHI-0003", "阶段": "在库", "驾驶员": "赵德柱",
     "桩号": "K0+000", "里程": 96300,
     "更新时间": "2026-09-29 16:30", "出勤日期": "2026-09-29"},
]

LEGACY_EMERGENCY = [
    {"车辆编号": "VEHI-0002", "阶段": "响应中", "驾驶员": "李海燕",
     "任务": "K31+800 防汛排水应急", "桩号": "K31+800", "里程": 205110,
     "更新时间": "2026-09-30 09:50", "出勤日期": "2026-09-30",
     "是否未结": True, "预警级别": "橙色"},
]


class DispatchService:
    """调度时序服务：占用、推进、释放、迁移与三页投影都收在这里。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vehicles: dict[str, dict[str, Any]] = {}
        # message_id -> (vehicle_code, seq)，去重后再应用
        self._messages: dict[str, tuple[str, int]] = {}
        self._migrated = False
        self._migrated_at: str | None = None
        self._migration_report: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 初始化
    def _ensure_loaded(self) -> None:
        """把车辆登记模块（vehicle）里的车辆同步进时序聚合。

        时序不复制车辆档案，只按车辆编号挂调度状态；这样新登记的车辆也能被调度。
        """
        for row in store.rows("vehicle"):
            code = str(row.get("车辆编号") or "").strip()
            if not code or code in self._vehicles:
                continue
            mileage_raw = row.get("当前里程")
            try:
                odometer = float(str(mileage_raw).replace("km", "").strip())
            except (TypeError, ValueError):
                odometer = 0.0
            self._vehicles[code] = {
                "id": int(row.get("id", 0)),
                "code": code,
                "plate": row.get("车牌号", ""),
                "type": row.get("车辆类型", ""),
                "unit": row.get("所属单位", ""),
                "odometer": odometer,
                "stage": STAGE_STANDBY,
                "version": 0,
                "occupier": None,          # 占用者：{operator, task, at}
                "timeline": [],            # 连续节点（唯一事实）
                "tasks": [],               # 本车任务（含未结/已完成/已取消）
            }

    def _get(self, code: str) -> dict[str, Any]:
        self._ensure_loaded()
        vehicle = self._vehicles.get(code)
        if vehicle is None:
            raise ConflictError(f"车辆 {code} 未登记，无法调度")
        return vehicle

    def _append_node(self, vehicle: dict[str, Any], node_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        seq = len(vehicle["timeline"]) + 1
        node = {
            "seq": seq,
            "type": node_type,
            "at": now_ts(),
            "stage_after": vehicle["stage"],
            **payload,
        }
        vehicle["timeline"].append(node)
        return node

    def _check_version(self, vehicle: dict[str, Any], expected: int) -> None:
        if int(expected) != int(vehicle["version"]):
            raise ConflictError(
                f"车辆 {vehicle['code']} 版本已变更（当前 v{vehicle['version']}，"
                f"提交依据 v{expected}），本次调度未生效，也未释放既有占用"
            )

    def _dedup(self, message_id: str | None) -> None:
        """消息去重：同一 message_id 的重复投递直接拒绝，不重复应用。"""
        mid = str(message_id or "").strip()
        if not mid:
            raise ConflictError("调度消息缺少 message_id，拒绝应用以避免重复执行")
        if mid in self._messages:
            code, seq = self._messages[mid]
            raise ConflictError(f"消息 {mid[:8]} 已应用于 {code} 节点 #{seq}，重复消息已丢弃")

    def _commit_message(self, message_id: str, vehicle: dict[str, Any]) -> None:
        """应用成功后再登记消息与版本，保证去重记录与业务结果一致。"""
        vehicle["version"] += 1
        self._messages[str(message_id)] = (vehicle["code"], len(vehicle["timeline"]))

    # ------------------------------------------------------------------ 写入
    def occupy(self, values: dict[str, Any]) -> dict[str, Any]:
        """占用车辆 + 挂驾驶员/任务首节点：同一把锁内原子成功或整体失败。"""
        code = str(values.get("vehicle_code") or "").strip()
        driver = str(values.get("driver") or "").strip()
        task_name = str(values.get("task") or "").strip()
        stake = str(values.get("stake") or "").strip()
        is_emergency = bool(values.get("is_emergency"))
        operator = str(values.get("operator") or "调度员").strip()
        message_id = str(values.get("message_id") or "").strip()
        expected = values.get("version")

        with self._lock:
            self._dedup(message_id)
            vehicle = self._get(code)
            if expected is None:
                raise ConflictError("占用车辆必须携带当前版本号")
            self._check_version(vehicle, int(expected))
            if not driver:
                raise ConflictError("占用车辆需要指定驾驶员")
            if not task_name:
                raise ConflictError("占用车辆需要指定任务名称")
            if vehicle["stage"] == STAGE_REPAIR:
                raise ConflictError(f"车辆 {code} 处于维修状态，不可占用")
            if vehicle["stage"] in OCCUPYING_STAGES:
                holder = vehicle["occupier"] or {}
                raise ConflictError(
                    f"车辆 {code} 已被任务「{holder.get('task', '未知任务')}」占用"
                    f"（{holder.get('operator', '未知调度')}），当前版本 v{vehicle['version']}，"
                    f"并发请求未生效"
                )

            # —— 以下为原子提交：占用标记与首节点同时落地 ——
            vehicle["stage"] = STAGE_ASSIGNED
            task = {
                "task_id": f"T-{new_message_id()[:8]}",
                "name": task_name,
                "driver": driver,
                "stake": stake,
                "emergency": is_emergency,
                "status": TASK_OPEN,
                "opened_at": now_ts(),
                "opened_by": operator,
                "closed_at": None,
            }
            vehicle["tasks"].append(task)
            vehicle["occupier"] = {"operator": operator, "task": task_name,
                                   "task_id": task["task_id"], "at": now_ts()}
            self._append_node(vehicle, NODE_DRIVER, {"驾驶员": driver, "动作": "派车", "调度员": operator})
            self._append_node(vehicle, NODE_TASK,
                              {"任务": task_name, "任务编号": task["task_id"],
                               "应急": is_emergency, "任务状态": TASK_OPEN,
                               "桩号": stake})
            self._commit_message(message_id, vehicle)
            return self.snapshot(vehicle)

    def advance(self, values: dict[str, Any]) -> dict[str, Any]:
        """推进节点：已派车→已出车（里程）→到场作业（桩号）。"""
        code = str(values.get("vehicle_code") or "").strip()
        target = str(values.get("target_stage") or "").strip()
        operator = str(values.get("operator") or "调度员").strip()
        message_id = str(values.get("message_id") or "").strip()
        expected = values.get("version")

        with self._lock:
            self._dedup(message_id)
            vehicle = self._get(code)
            if expected is None:
                raise ConflictError("推进节点必须携带当前版本号")
            self._check_version(vehicle, int(expected))
            if vehicle["stage"] not in OCCUPYING_STAGES:
                raise ConflictError(f"车辆 {code} 当前为{vehicle['stage']}，没有可推进的占用任务")

            stage = vehicle["stage"]
            if target == STAGE_DEPARTED:
                if stage != STAGE_ASSIGNED:
                    raise ConflictError(f"车辆 {code} 当前为{stage}，不能直接推进为{STAGE_DEPARTED}")
                mileage = self._as_mileage(values.get("mileage"), vehicle)
                vehicle["stage"] = STAGE_DEPARTED
                self._append_node(vehicle, NODE_MILEAGE,
                                  {"里程": mileage, "动作": "出车离场", "调度员": operator,
                                   "表显增量": round(mileage - vehicle["odometer"], 1)})
                vehicle["odometer"] = mileage
            elif target == STAGE_ONSITE:
                if stage != STAGE_DEPARTED:
                    raise ConflictError(f"车辆 {code} 当前为{stage}，须先出车再到场")
                stake = str(values.get("stake") or "").strip()
                if not stake:
                    raise ConflictError("到场作业必须上报作业桩号")
                vehicle["stage"] = STAGE_ONSITE
                self._append_node(vehicle, NODE_STAKE,
                                  {"桩号": stake, "动作": "到场作业", "调度员": operator})
            else:
                raise ConflictError(f"目标阶段「{target}」不是可推进的作业节点")

            self._commit_message(message_id, vehicle)
            return self.snapshot(vehicle)

    def release(self, values: dict[str, Any]) -> dict[str, Any]:
        """收车归库 / 撤销派车：只有占用者本人凭正确版本号才能释放。

        释放成功后任务结单、车辆回待命；任何校验失败都不改动占用状态。
        """
        code = str(values.get("vehicle_code") or "").strip()
        operator = str(values.get("operator") or "调度员").strip()
        message_id = str(values.get("message_id") or "").strip()
        expected = values.get("version")
        cancel = bool(values.get("cancel"))

        with self._lock:
            self._dedup(message_id)
            vehicle = self._get(code)
            if expected is None:
                raise ConflictError("释放车辆必须携带当前版本号")
            self._check_version(vehicle, int(expected))
            if vehicle["stage"] not in OCCUPYING_STAGES:
                raise ConflictError(f"车辆 {code} 当前为{vehicle['stage']}，未被占用，无需释放")
            holder = vehicle["occupier"] or {}
            if holder.get("operator") != operator:
                # 关键：不能释放他人占用
                raise ConflictError(
                    f"车辆 {code} 由 {holder.get('operator', '他人')} 占用，"
                    f"{operator} 无权释放；占用保持不变"
                )

            task = next((t for t in reversed(vehicle["tasks"]) if t["status"] == TASK_OPEN), None)
            if cancel:
                # 撤销派车：任务取消，无收车里程
                if task is not None:
                    task["status"] = TASK_CANCELLED
                    task["closed_at"] = now_ts()
                self._append_node(vehicle, NODE_TASK,
                                  {"任务": task["name"] if task else holder.get("task", ""),
                                   "任务编号": task["task_id"] if task else holder.get("task_id", ""),
                                   "任务状态": TASK_CANCELLED, "动作": "撤销派车",
                                   "调度员": operator})
            else:
                mileage = self._as_mileage(values.get("mileage"), vehicle, allow_equal=True)
                vehicle["odometer"] = mileage
                self._append_node(vehicle, NODE_MILEAGE,
                                  {"里程": mileage, "动作": "收车归库", "调度员": operator})
                if task is not None:
                    task["status"] = TASK_DONE
                    task["closed_at"] = now_ts()
                self._append_node(vehicle, NODE_TASK,
                                  {"任务": task["name"] if task else holder.get("task", ""),
                                   "任务编号": task["task_id"] if task else holder.get("task_id", ""),
                                   "任务状态": TASK_DONE, "动作": "结单归库",
                                   "调度员": operator})

            vehicle["stage"] = STAGE_STANDBY
            vehicle["occupier"] = None
            self._commit_message(message_id, vehicle)
            return self.snapshot(vehicle)

    @staticmethod
    def _as_mileage(raw: Any, vehicle: dict[str, Any], *, allow_equal: bool = False) -> float:
        try:
            mileage = float(raw)
        except (TypeError, ValueError):
            raise ConflictError("里程节点必须携带数值型表显里程")
        current = float(vehicle["odometer"])
        if mileage < current or (mileage == current and not allow_equal):
            raise ConflictError(
                f"车辆 {vehicle['code']} 里程不能回退：当前表显 {current}，上报 {mileage}"
            )
        return mileage

    # ------------------------------------------------------------------ 迁移
    def migration_preview(self) -> dict[str, Any]:
        """转换前逐车比对：排班表、轨迹回放、应急汇总三页阶段摆在一起给出冲突清单。"""
        with self._lock:
            self._ensure_loaded()
            schedule = {r["车辆编号"]: r for r in LEGACY_SCHEDULE}
            tracks = {r["车辆编号"]: r for r in LEGACY_TRACKS}
            emergency = {r["车辆编号"]: r for r in LEGACY_EMERGENCY}

            per_vehicle: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
            codes = sorted(set(schedule) | set(tracks) | set(emergency) | set(self._vehicles))
            for code in codes:
                s_row, t_row, e_row = schedule.get(code), tracks.get(code), emergency.get(code)
                mapped = []
                sources: dict[str, str] = {}
                for label, row in (("排班表", s_row), ("轨迹回放", t_row), ("应急汇总", e_row)):
                    if row is None:
                        continue
                    stage = LEGACY_STAGE_MAP.get(str(row["阶段"]), str(row["阶段"]))
                    mapped.append(stage)
                    sources[label] = stage
                # 同一辆车三页阶段映射后不一致即为流程断点
                divergent = sorted(set(mapped))
                is_conflict = len(divergent) > 1
                open_emergency = bool(e_row and e_row.get("是否未结"))
                per_vehicle.append({
                    "vehicle_code": code,
                    "driver": (s_row or t_row or e_row or {}).get("驾驶员", ""),
                    "sources": sources,
                    "conflict": is_conflict,
                    "未结应急任务": open_emergency,
                    "收敛阶段": self._converge_stage(s_row, t_row, e_row),
                })
                if is_conflict:
                    conflicts.append({"vehicle_code": code, "sources": sources,
                                      "收敛阶段": self._converge_stage(s_row, t_row, e_row)})
            return {
                "migrated": self._migrated,
                "migrated_at": self._migrated_at,
                "vehicles_checked": len(codes),
                "conflict_count": len(conflicts),
                "open_task_count": sum(1 for v in per_vehicle if v["未结应急任务"]),
                "conflicts": conflicts,
                "per_vehicle": per_vehicle,
            }

    @staticmethod
    def _converge_stage(s_row: dict[str, Any] | None, t_row: dict[str, Any] | None,
                        e_row: dict[str, Any] | None) -> str:
        """冲突收敛口径：取三页中最靠后的作业阶段（作业中 > 行驶中 > 已排班）。"""
        rank = {STAGE_STANDBY: 0, STAGE_ASSIGNED: 1, STAGE_DEPARTED: 2, STAGE_ONSITE: 3}
        stages = [LEGACY_STAGE_MAP.get(str(r["阶段"]), str(r["阶段"]))
                  for r in (s_row, t_row, e_row) if r is not None]
        return max(stages, key=lambda s: rank.get(s, -1)) if stages else STAGE_STANDBY

    def migrate(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行转换（幂等）：历史出勤按原驾驶员/车辆快照保留，未结任务迁入新时序。"""
        values = values or {}
        message_id = str(values.get("message_id") or new_message_id()).strip()
        with self._lock:
            self._dedup(message_id)
            preview = self.migration_preview()
            if self._migrated:
                report = dict(self._migration_report or {})
                report["idempotent"] = True
                return report

            history: list[dict[str, Any]] = []
            moved_open: list[dict[str, Any]] = []
            for item in preview["per_vehicle"]:
                code = item["vehicle_code"]
                vehicle = self._vehicles.get(code)
                if vehicle is None:
                    continue
                s_row = next((r for r in LEGACY_SCHEDULE if r["车辆编号"] == code), None)
                t_row = next((r for r in LEGACY_TRACKS if r["车辆编号"] == code), None)
                e_row = next((r for r in LEGACY_EMERGENCY if r["车辆编号"] == code), None)
                driver = item.get("driver") or ""
                task_name = ((e_row or s_row or {}).get("任务")) or f"{driver} 历史出勤"
                stake = ((e_row or t_row or s_row or {}).get("桩号")) or ""
                duty_date = ((s_row or t_row or e_row or {}).get("出勤日期")) or ""

                # 1) 历史出勤冻结为快照：驾驶员与车辆信息按当时原样保留
                snapshot = {
                    "vehicle_code": code,
                    "plate": vehicle["plate"],
                    "vehicle_type": vehicle["type"],
                    "driver_snapshot": driver,
                    "vehicle_snapshot": {
                        "车牌号": vehicle["plate"], "车辆类型": vehicle["type"],
                        "所属单位": vehicle["unit"], "车辆编号": code,
                    },
                    "出勤日期": duty_date,
                    "任务": task_name,
                    "桩号": stake,
                    "排班阶段": s_row["阶段"] if s_row else None,
                    "轨迹阶段": t_row["阶段"] if t_row else None,
                    "应急阶段": e_row["阶段"] if e_row else None,
                    "sources": item["sources"],
                    "conflict": item["conflict"],
                    "archived_at": now_ts(),
                }
                history.append(snapshot)

                # 2) 未结任务迁入新时序：从最靠后的已知阶段重建连续节点
                if item["未结应急任务"]:
                    stage = item["收敛阶段"]
                    vehicle["stage"] = stage
                    task = {
                        "task_id": f"T-MIG-{new_message_id()[:8]}",
                        "name": task_name,
                        "driver": driver,
                        "stake": stake,
                        "emergency": True,
                        "status": TASK_OPEN,
                        "opened_at": f"{duty_date} 00:00:00" if duty_date else now_ts(),
                        "opened_by": "历史迁移",
                        "closed_at": None,
                        "migrated_from": "应急汇总",
                        "alert_level": (e_row or {}).get("预警级别"),
                    }
                    vehicle["tasks"].append(task)
                    vehicle["occupier"] = {"operator": "历史迁移", "task": task_name,
                                           "task_id": task["task_id"], "at": task["opened_at"]}
                    self._append_node(vehicle, NODE_DRIVER,
                                      {"驾驶员": driver, "动作": "历史出勤迁入",
                                       "调度员": "历史迁移", "快照": True})
                    self._append_node(vehicle, NODE_TASK,
                                      {"任务": task_name, "任务编号": task["task_id"],
                                       "应急": True, "任务状态": TASK_OPEN, "桩号": stake,
                                       "快照": True})
                    if stage in (STAGE_DEPARTED, STAGE_ONSITE):
                        mileage = float((t_row or e_row or {}).get("里程") or vehicle["odometer"])
                        vehicle["odometer"] = mileage
                        self._append_node(vehicle, NODE_MILEAGE,
                                          {"里程": mileage, "动作": "历史出车补录",
                                           "调度员": "历史迁移", "快照": True})
                    if stage == STAGE_ONSITE:
                        self._append_node(vehicle, NODE_STAKE,
                                          {"桩号": stake, "动作": "历史到场补录",
                                           "调度员": "历史迁移", "快照": True})
                    vehicle["version"] += 1
                    moved_open.append({"vehicle_code": code, "task": task_name,
                                        "driver": driver, "stage": stage,
                                        "task_id": task["task_id"]})
                else:
                    # 已完结出勤不占用车辆，车辆回到待命
                    vehicle["stage"] = STAGE_STANDBY
                    vehicle["occupier"] = None

            self._migrated = True
            self._migrated_at = now_ts()
            self._migration_report = {
                "migrated": True,
                "migrated_at": self._migrated_at,
                "message_id": message_id,
                "vehicles_checked": preview["vehicles_checked"],
                "conflict_count": preview["conflict_count"],
                "conflicts": preview["conflicts"],
                "history_snapshots": history,
                "moved_open_tasks": moved_open,
                "message": (f"转换完成：比对 {preview['vehicles_checked']} 辆车，"
                            f"发现 {preview['conflict_count']} 处分页阶段冲突；"
                            f"历史出勤 {len(history)} 条按快照归档，"
                            f"未结任务 {len(moved_open)} 条迁入新时序"),
            }
            self._messages[message_id] = ("__migration__", len(self._messages) + 1)
            return self._migration_report

    def is_migrated(self) -> bool:
        with self._lock:
            return self._migrated

    # ------------------------------------------------------------------ 接管
    def takeover(self, values: dict[str, Any]) -> dict[str, Any]:
        """接管迁移进来的未结任务占用。

        仅允许接管「历史迁移」挂名的占用；现职调度员的占用不允许被抢。
        接管本身同样是 CAS + 去重的原子写入，接管后版本号 +1。
        """
        code = str(values.get("vehicle_code") or "").strip()
        operator = str(values.get("operator") or "").strip()
        message_id = str(values.get("message_id") or "").strip()
        expected = values.get("version")

        with self._lock:
            self._dedup(message_id)
            vehicle = self._get(code)
            if not operator:
                raise ConflictError("接管占用需要登记接管调度员")
            if expected is None:
                raise ConflictError("接管占用必须携带当前版本号")
            self._check_version(vehicle, int(expected))
            if vehicle["stage"] not in OCCUPYING_STAGES:
                raise ConflictError(f"车辆 {code} 当前为{vehicle['stage']}，没有可接管的占用")
            holder = vehicle["occupier"] or {}
            if holder.get("operator") != "历史迁移":
                raise ConflictError(
                    f"车辆 {code} 由 {holder.get('operator', '他人')} 实际占用，"
                    f"接管仅适用于历史迁移任务，现职占用不可抢"
                )
            task = next((t for t in reversed(vehicle["tasks"]) if t["status"] == TASK_OPEN), None)
            if task is not None:
                task["opened_by"] = f"{operator}（接管历史迁移）"
            vehicle["occupier"] = {"operator": operator, "task": holder.get("task", ""),
                                   "task_id": holder.get("task_id", ""), "at": now_ts(),
                                   "taken_over_from": "历史迁移"}
            self._append_node(vehicle, NODE_DRIVER,
                              {"驾驶员": (task or {}).get("driver", ""),
                               "动作": "接管历史任务", "调度员": operator})
            self._commit_message(message_id, vehicle)
            return self.snapshot(vehicle)

    # ------------------------------------------------------------------ 档案联动
    def set_registry_stage(self, code: str, stage: str) -> None:
        """车辆档案侧维修/在库联动：仅空闲车辆可进入维修，不产生时序节点。"""
        with self._lock:
            vehicle = self._get(code)
            if stage == STAGE_REPAIR:
                if vehicle["stage"] in OCCUPYING_STAGES:
                    raise ConflictError(
                        f"车辆 {code} 被占用中，不能送修；请先由占用者收车归库"
                    )
                vehicle["stage"] = STAGE_REPAIR
                vehicle["version"] += 1
            elif stage == STAGE_STANDBY and vehicle["stage"] == STAGE_REPAIR:
                vehicle["stage"] = STAGE_STANDBY
                vehicle["version"] += 1

    # ------------------------------------------------------------------ 投影
    def snapshot(self, vehicle: dict[str, Any]) -> dict[str, Any]:
        """单辆车的时序快照：阶段、占用、版本与连续节点。"""
        last_node = vehicle["timeline"][-1] if vehicle["timeline"] else None
        return {
            "id": vehicle["id"],
            "vehicle_code": vehicle["code"],
            "车牌号": vehicle["plate"],
            "车辆类型": vehicle["type"],
            "所属单位": vehicle["unit"],
            "stage": vehicle["stage"],
            "occupied": vehicle["stage"] in OCCUPYING_STAGES,
            "version": vehicle["version"],
            "occupier": vehicle["occupier"],
            "odometer": vehicle["odometer"],
            "current_driver": (last_node or {}).get("驾驶员")
            or (vehicle["occupier"] or {}).get("operator"),
            "last_node": last_node,
            "timeline": list(vehicle["timeline"]),
            "tasks": list(vehicle["tasks"]),
        }

    def list_vehicles(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            return [self.snapshot(v) for v in sorted(self._vehicles.values(), key=lambda x: x["code"])]

    def get_vehicle(self, code: str) -> dict[str, Any]:
        with self._lock:
            return self.snapshot(self._get(code))

    def schedule_ledger(self) -> dict[str, Any]:
        """排班台账：时序投影。每行的阶段直接回写自时序，杜绝台账另写一套。"""
        with self._lock:
            rows = []
            for v in self.list_vehicles():
                open_task = next((t for t in reversed(v["tasks"]) if t["status"] == TASK_OPEN), None)
                last_stake = next((n for n in reversed(v["timeline"]) if n["type"] == NODE_STAKE), None)
                rows.append({
                    "车辆编号": v["vehicle_code"],
                    "车牌号": v["车牌号"],
                    "排班阶段": v["stage"],
                    "占用": "是" if v["occupied"] else "否",
                    "驾驶员": open_task["driver"] if open_task else "—",
                    "任务": open_task["name"] if open_task else "—",
                    "桩号": (open_task or {}).get("stake")
                    or (last_stake or {}).get("桩号", "—"),
                    "version": v["version"],
                    "更新时间": (v["last_node"] or {}).get("at", ""),
                })
            return {"source": "调度时序", "migrated": self._migrated, "items": rows}

    def emergency_list(self) -> dict[str, Any]:
        """应急清单：仅投影未结应急任务，阶段与时序完全一致。"""
        with self._lock:
            rows = []
            for v in self.list_vehicles():
                for task in v["tasks"]:
                    if task["emergency"] and task["status"] == TASK_OPEN:
                        rows.append({
                            "车辆编号": v["vehicle_code"],
                            "应急阶段": v["stage"],
                            "驾驶员": task["driver"],
                            "任务": task["name"],
                            "桩号": task["stake"],
                            "预警级别": task.get("alert_level", "—"),
                            "占用": "是",
                            "version": v["version"],
                            "任务编号": task["task_id"],
                        })
            return {"source": "调度时序", "migrated": self._migrated, "items": rows}

    def track_detail(self, code: str | None = None) -> dict[str, Any]:
        """轨迹详情：把时序节点展开成轨迹点（驾驶员→任务→里程→桩号连续）。"""
        with self._lock:
            vehicles = [self.get_vehicle(code)] if code else self.list_vehicles()
            items = []
            for v in vehicles:
                items.append({
                    "车辆编号": v["vehicle_code"],
                    "轨迹阶段": v["stage"],
                    "version": v["version"],
                    "当前里程": v["odometer"],
                    "占用": "是" if v["occupied"] else "否",
                    "track": [{
                        "seq": n["seq"], "type": n["type"], "at": n["at"],
                        "stage_after": n["stage_after"],
                        "驾驶员": n.get("驾驶员"), "任务": n.get("任务"),
                        "桩号": n.get("桩号"), "里程": n.get("里程"),
                        "动作": n.get("动作"),
                    } for n in v["timeline"]],
                })
            return {"source": "调度时序", "migrated": self._migrated, "items": items}

    def history(self, code: str | None = None) -> dict[str, Any]:
        """历史出勤：转换时冻结的驾驶员/车辆快照，只读。"""
        items = list((self._migration_report or {}).get("history_snapshots", []))
        if code:
            items = [r for r in items if r["vehicle_code"] == code]
        return {"items": items, "total": len(items)}


dispatch_service = DispatchService()
