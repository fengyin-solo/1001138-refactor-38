"""车辆调度时序图核心域。

重构背景：同一辆养护车在「排班表、轨迹回放、应急汇总」三处各自维护阶段，
出现流程断点，调度员无法判断车辆是否被占用。这里把车辆重建为聚合根，
驾驶员、任务、桩号、里程是沿时间轴连续推进的节点，阶段只在时序上计算一次，
再原子回写到排班台账、应急清单与轨迹详情。

并发口径：
- 所有写操作在同一把可重入锁内完成「占用车辆 + 推进节点」，二者原子成功；
- 命令消息先按 message_id 去重，再落库应用；
- 调度必须携带读取时的版本号（CAS），并发调度只认可先取得版本号的一方；
- 释放占用必须出示占用令牌，未成功推进的一方不能释放他人的占用。

历史口径：
- 历史出勤按「原驾驶员 + 原车辆」快照冻结，迁移后不再被时序推进改写；
- 仅未结任务迁入新时序；转换前逐车生成差异比对，确认后才执行迁移。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "dispatch"

# 时序阶段：节点沿该序列推进，阶段是时序的投影，三处页面不再各自定义
STAGE_AVAILABLE = "在库待命"
STAGE_DISPATCHED = "已派车"
STAGE_ENROUTE = "赶赴现场"
STAGE_ONSITE = "现场作业"
STAGE_EMERGENCY = "应急处置"
STAGE_PATROL = "巡查归队"
STAGE_RETURNED = "收车归库"
STAGE_MAINTAIN = "维修中"

NODE_STAGES = [STAGE_DISPATCHED, STAGE_ENROUTE, STAGE_ONSITE, STAGE_EMERGENCY, STAGE_PATROL]
# 收尾阶段不参与线性序号比对：可从任一占用阶段直接收车/送修；
# 中间作业阶段（现场作业/应急处置/巡查归队）按业务实际选择，推进时只允许前进一格
TERMINAL_STAGES = [STAGE_RETURNED, STAGE_MAINTAIN]
OCCUPIED_STAGES = NODE_STAGES  # 处于推进序列中的车辆一律视为已占用

# 节点类型与可携带的连续要素
NODE_DISPATCH = "派车"
NODE_ADVANCE = "推进"
NODE_RETURN = "收车"
NODE_REPAIR = "送修"
NODE_REPAIRED = "修复"

KIND_DISPLAY = {
    NODE_DISPATCH: "派车占用",
    NODE_ADVANCE: "阶段推进",
    NODE_RETURN: "收车归库",
    NODE_REPAIR: "送修",
    NODE_REPAIRED: "修复待命",
}

DISPATCH_KINDS = {"排班", "应急"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id(table: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in table), default=0) + 1


class ConflictError(Exception):
    """并发冲突：占用已被他人取得，或版本号已过期。"""


class DomainError(Exception):
    """业务规则不满足。"""


class DispatchService:
    """调度时序应用服务；所有变更在同一把锁内原子完成。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._booted = False

    # ------------------------------------------------------------------ 启动数据
    def boot(self) -> None:
        """首次使用时把重构前的三处断点数据铺进仓库（只铺一次）。

        排班表、应急清单、轨迹回放此前各自记录阶段，刻意保留差异，
        供「转换前逐车比对」复现流程断点。
        """
        with self._lock:
            if self._booted:
                return
            self._booted = True
            if store.rows("dispatch_meta"):
                return
            store.rows("dispatch_meta").append({"id": 1, "migrated": False, "migrated_at": None})

            store.rows("dispatch_vehicle").extend([
                {"id": 1, "车辆编号": "VEHI-1001", "车牌号": "沪A·A1001",
                 "车辆类型": "综合养护车", "所属单位": "道桥养护一所", "基准里程": 52130},
                {"id": 2, "车辆编号": "VEHI-1002", "车牌号": "沪B·B2002",
                 "车辆类型": "除雪防滑车", "所属单位": "道桥养护二所", "基准里程": 48620},
                {"id": 3, "车辆编号": "VEHI-1003", "车牌号": "沪C·C3003",
                 "车辆类型": "防汛排涝车", "所属单位": "应急抢险中心", "基准里程": 60340},
                {"id": 4, "车辆编号": "VEHI-1004", "车牌号": "沪D·D4004",
                 "车辆类型": "巡查车", "所属单位": "道桥养护一所", "基准里程": 31800},
            ])

            # 重构前三处独立台账：同一车辆的阶段彼此不一致，即流程断点
            store.rows("dispatch_legacy_schedule").extend([
                {"id": 1, "车辆编号": "VEHI-1001", "驾驶员": "王建国",
                 "任务名称": "K12+300 伸缩缝保养", "任务类别": "排班",
                 "台账阶段": "已派车", "未结": True, "记录日期": "2026-09-30"},
                {"id": 2, "车辆编号": "VEHI-1002", "驾驶员": "李秀兰",
                 "任务名称": "G318 K28 除雪巡逻", "任务类别": "排班",
                 "台账阶段": "待派车", "未结": True, "记录日期": "2026-09-30"},
                {"id": 3, "车辆编号": "VEHI-1003", "驾驶员": "赵强",
                 "任务名称": "K7+800 积水强排", "任务类别": "应急",
                 "台账阶段": "现场作业", "未结": True, "记录日期": "2026-09-30"},
            ])
            store.rows("dispatch_legacy_emergency").extend([
                {"id": 1, "车辆编号": "VEHI-1001", "驾驶员": "王建国",
                 "任务名称": "K12+300 伸缩缝保养", "应急清单阶段": "—",
                 "未结": True, "记录日期": "2026-09-30"},
                {"id": 2, "车辆编号": "VEHI-1003", "驾驶员": "赵强",
                 "任务名称": "K7+800 积水强排", "应急清单阶段": "已派车",
                 "未结": True, "记录日期": "2026-09-30"},
            ])
            store.rows("dispatch_legacy_track").extend([
                {"id": 1, "车辆编号": "VEHI-1001", "驾驶员": "王建国",
                 "任务名称": "K12+300 伸缩缝保养", "轨迹阶段": "赶赴现场",
                 "最新桩号": "K11+900", "最新里程": 52148, "未结": True,
                 "记录日期": "2026-09-30"},
                {"id": 2, "车辆编号": "VEHI-1002", "驾驶员": "李秀兰",
                 "任务名称": "G318 K28 除雪巡逻", "轨迹阶段": "现场作业",
                 "最新桩号": "G318 K27+600", "最新里程": 48702, "未结": True,
                 "记录日期": "2026-09-30"},
                {"id": 3, "车辆编号": "VEHI-1003", "驾驶员": "赵强",
                 "任务名称": "K7+800 积水强排", "轨迹阶段": "收车归库",
                 "最新桩号": "K7+800", "最新里程": 60455, "未结": True,
                 "记录日期": "2026-09-30"},
            ])
            # 重构前的历史出勤：迁移时按原驾驶员+原车辆快照保留
            store.rows("dispatch_legacy_attendance").extend([
                {"id": 1, "车辆编号": "VEHI-1001", "驾驶员": "王建国",
                 "任务名称": "K10+200 支座更换", "出勤日期": "2026-08-12",
                 "收车里程": 51920, "办结": True},
                {"id": 2, "车辆编号": "VEHI-1002", "驾驶员": "李秀兰",
                 "任务名称": "G318 K30 除雪保通", "出勤日期": "2026-01-22",
                 "收车里程": 48110, "办结": True},
            ])

    def _meta(self) -> dict[str, Any]:
        return store.rows("dispatch_meta")[0]

    def is_migrated(self) -> bool:
        self.boot()
        return bool(self._meta().get("migrated"))

    # ------------------------------------------------------------- 时序装配视图
    def _timelines(self) -> dict[int, dict[str, Any]]:
        """把节点表按车辆装配成当前时序（不落库的投影）。"""
        result: dict[int, dict[str, Any]] = {}
        nodes = sorted(store.rows("dispatch_node"), key=lambda n: int(n["id"]))
        for node in nodes:
            vid = int(node["车辆id"])
            timeline = result.setdefault(vid, {"车辆id": vid, "nodes": []})
            timeline["nodes"].append(node)
        return result

    def _vehicle_view(self, vehicle: dict[str, Any],
                      timeline: dict[str, Any] | None) -> dict[str, Any]:
        """计算单辆车的当前阶段与连续要素；阶段只在这里算一次。"""
        nodes = timeline["nodes"] if timeline else []
        current: dict[str, Any] | None = nodes[-1] if nodes else None
        if current is None:
            stage = STAGE_AVAILABLE
            stage_index = -1
            occupied = False
        else:
            stage = str(current["阶段"])
            stage_index = current.get("阶段序号", -1)
            occupied = stage in OCCUPIED_STAGES

        view = {k: vehicle.get(k) for k in ("id", "车辆编号", "车牌号", "车辆类型", "所属单位")}
        view.update({
            "version": int(vehicle.get("version", 0)),
            "stage": stage,
            "stage_index": stage_index,
            "occupied": occupied,
            "占用令牌": current.get("占用令牌") if occupied else None,
            "驾驶员": current.get("驾驶员") if current else None,
            "任务名称": current.get("任务名称") if current else None,
            "任务类别": current.get("任务类别") if current else None,
            "桩号": current.get("桩号") if current else None,
            "里程": current.get("里程") if current else None,
            "更新时间": current.get("时间") if current else None,
            "节点数": len(nodes),
        })
        return view

    def list_timelines(self, *, keyword: str | None = None,
                       only_occupied: bool = False) -> list[dict[str, Any]]:
        """调度时序图列表：每辆车一行，阶段与时序一致。"""
        self.boot()
        with self._lock:
            assembled = self._timelines()
            views = [self._vehicle_view(v, assembled.get(int(v["id"])))
                     for v in store.rows("dispatch_vehicle")]
        if keyword:
            views = [v for v in views
                     if keyword in str(v["车辆编号"]) or keyword in str(v["车牌号"])
                     or keyword in str(v.get("驾驶员") or "")]
        if only_occupied:
            views = [v for v in views if v["occupied"]]
        return views

    def get_timeline(self, vehicle_id: int) -> dict[str, Any] | None:
        """时序详情：车辆头信息 + 连续节点（驾驶员/任务/桩号/里程）+ 三处回写。"""
        self.boot()
        with self._lock:
            vehicle = store.find("dispatch_vehicle", vehicle_id)
            if vehicle is None:
                return None
            assembled = self._timelines().get(vehicle_id)
            head = self._vehicle_view(vehicle, assembled)
            nodes = [self._public_node(n) for n in (assembled["nodes"] if assembled else [])]
            return {
                "vehicle": head,
                "nodes": nodes,
                "writeback": {
                    "schedule": self._schedule_snapshot(vehicle_id),
                    "emergency": self._emergency_snapshot(vehicle_id),
                    "track": self._track_snapshot(vehicle_id),
                },
            }

    @staticmethod
    def _public_node(node: dict[str, Any]) -> dict[str, Any]:
        return {k: node.get(k) for k in (
            "id", "kind", "kind_label", "阶段", "阶段序号", "驾驶员", "任务名称",
            "任务类别", "桩号", "里程", "时间", "message_id")}

    # ------------------------------------------------------------- 回写投影
    def _schedule_snapshot(self, vehicle_id: int) -> dict[str, Any] | None:
        """返回台账中的原行对象（回写时需要原地更新，不能给拷贝）。"""
        for row in store.rows("dispatch_schedule"):
            if int(row["车辆id"]) == vehicle_id:
                return row
        return None

    def _emergency_snapshot(self, vehicle_id: int) -> dict[str, Any] | None:
        """返回应急清单中的原行对象（回写时需要原地更新，不能给拷贝）。"""
        for row in store.rows("dispatch_emergency"):
            if int(row["车辆id"]) == vehicle_id:
                return row
        return None

    def _track_snapshot(self, vehicle_id: int) -> dict[str, Any] | None:
        rows = [r for r in store.rows("dispatch_track") if int(r["车辆id"]) == vehicle_id]
        return dict(rows[-1]) if rows else None

    def list_schedule(self) -> list[dict[str, Any]]:
        """排班台账：阶段直接取自时序投影。"""
        self.boot()
        with self._lock:
            return [dict(r) for r in store.rows("dispatch_schedule")]

    def list_emergency(self) -> list[dict[str, Any]]:
        """应急清单：阶段直接取自时序投影。"""
        self.boot()
        with self._lock:
            return [dict(r) for r in store.rows("dispatch_emergency")]

    def list_tracks(self) -> list[dict[str, Any]]:
        """轨迹详情（轨迹回放）：每车取最新节点，阶段/桩号/里程与时序一致。"""
        self.boot()
        with self._lock:
            latest: dict[int, dict[str, Any]] = {}
            for row in store.rows("dispatch_track"):
                latest[int(row["车辆id"])] = row
            return [dict(r) for r in latest.values()]

    def list_attendance(self) -> list[dict[str, Any]]:
        """历史出勤：迁移冻结的原驾驶员+原车辆快照。"""
        self.boot()
        with self._lock:
            return [dict(r) for r in store.rows("dispatch_attendance")]

    # ------------------------------------------------------------- 去重日志
    def list_messages(self) -> list[dict[str, Any]]:
        self.boot()
        with self._lock:
            rows = sorted(store.rows("dispatch_message"),
                          key=lambda m: int(m["id"]), reverse=True)
            return [dict(r) for r in rows]

    def _record_message(self, message_id: str | None, command: str,
                        status: str, detail: str) -> dict[str, Any]:
        table = store.rows("dispatch_message")
        rec = {
            "id": _new_id(table),
            "message_id": message_id or "",
            "command": command,
            "status": status,
            "detail": detail,
            "time": _now(),
        }
        table.append(rec)
        return rec

    # ------------------------------------------------------------- 命令应用
    def apply(self, command: str, payload: dict[str, Any],
              message_id: str | None) -> dict[str, Any]:
        """命令入口：去重后再应用；未见过的消息在锁内完成检查与落库。"""
        self.boot()
        mid = (message_id or "").strip() or None
        with self._lock:
            if mid:
                seen = next((r for r in store.rows("dispatch_message")
                             if r.get("message_id") == mid), None)
                if seen is not None:
                    # 去重命中：返回首次结果，不再二次应用
                    return {"duplicate": True, "message": dict(seen), "result": None}
            try:
                if command == "dispatch":
                    result = self._dispatch(payload)
                elif command == "advance":
                    result = self._advance(payload)
                elif command == "release":
                    result = self._release(payload)
                elif command == "repair_done":
                    result = self.repair_done(int(payload.get("vehicle_id") or 0))
                else:
                    raise DomainError(f"未知命令「{command}」")
            except ConflictError as exc:
                self._record_message(mid, command, "conflict", str(exc))
                raise
            except DomainError as exc:
                self._record_message(mid, command, "rejected", str(exc))
                raise
            self._record_message(mid, command, "applied", f"车辆 {result['车辆编号']} → {result['stage']}")
            return {"duplicate": False, "message": None, "result": result}

    @staticmethod
    def _require_text(payload: dict[str, Any], key: str, label: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise DomainError(f"{label}不能为空")
        return value

    def _append_node(self, vehicle: dict[str, Any], *, kind: str, stage: str,
                     payload: dict[str, Any], driver: str | None = None,
                     task_name: str | None = None, task_kind: str | None = None,
                     stake: Any = None, mileage: Any = None) -> dict[str, Any]:
        """追加节点并把连续要素（驾驶员/任务/桩号/里程）写入回写台账。

        调用方已持锁；节点与三处回写在同一临界区内完成。
        """
        nodes_table = store.rows("dispatch_node")
        assembled = self._timelines().get(int(vehicle["id"]))
        last = assembled["nodes"][-1] if assembled and assembled["nodes"] else None

        # 连续要素：未传则沿用上一节点，保证驾驶员/任务/桩号/里程是连续节点
        def inherit(value: Any, prev_key: str) -> Any:
            if value not in (None, ""):
                return value
            return last.get(prev_key) if last else None

        driver = inherit(driver, "驾驶员")
        task_name = inherit(task_name, "任务名称")
        task_kind = inherit(task_kind, "任务类别")
        stake = inherit(stake, "桩号")
        mileage = inherit(mileage, "里程")

        stage_index = NODE_STAGES.index(stage) if stage in NODE_STAGES else (
            len(NODE_STAGES) if stage in TERMINAL_STAGES else -1)

        token = None
        if stage in OCCUPIED_STAGES:
            # 同一趟占用任务的后续节点沿用原令牌；仅派车首节点签发新令牌
            previous_token = last.get("占用令牌") if last else None
            token = previous_token or uuid.uuid4().hex[:12]

        node = {
            "id": _new_id(nodes_table),
            "车辆id": int(vehicle["id"]),
            "kind": kind,
            "kind_label": KIND_DISPLAY.get(kind, kind),
            "阶段": stage,
            "阶段序号": stage_index,
            "驾驶员": driver,
            "任务名称": task_name,
            "任务类别": task_kind,
            "桩号": stake,
            "里程": mileage,
            "占用令牌": token,
            "message_id": payload.get("message_id") or "",
            "时间": _now(),
        }
        nodes_table.append(node)
        vehicle["version"] = int(vehicle.get("version", 0)) + 1
        self._write_back(vehicle, node)
        return node

    def _write_back(self, vehicle: dict[str, Any], node: dict[str, Any]) -> None:
        """阶段一处计算、三处回写：排班台账 / 应急清单 / 轨迹详情。

        回写行 id 统一借用节点 id：一次节点推进对应三处各一行，天然不撞号，
        也能直接从台账行反查到来源节点。
        """
        vid = int(vehicle["id"])
        stage = str(node["阶段"])
        row_id = int(node["id"])
        common = {
            "车辆id": vid,
            "车辆编号": vehicle["车辆编号"],
            "车牌号": vehicle["车牌号"],
            "驾驶员": node.get("驾驶员"),
            "任务名称": node.get("任务名称"),
            "任务类别": node.get("任务类别"),
            "阶段": stage,
            "占用中": stage in OCCUPIED_STAGES,
            "更新时间": node["时间"],
            "version": vehicle["version"],
        }

        # 收尾阶段（收车/送修）以及修复后回到待命，都意味着在途任务已办结；
        # 车辆初次待命时根本不存在台账行，不会受这里影响
        task_closed = stage in TERMINAL_STAGES or stage == STAGE_AVAILABLE

        # 排班台账：每辆车一条在途任务，收车/送修即办结
        schedule = self._schedule_snapshot(vid)
        if schedule is None:
            row = {"id": row_id}
            row.update(common)
            row["办结"] = task_closed
            store.rows("dispatch_schedule").append(row)
        else:
            schedule.update(common)
            schedule["办结"] = task_closed

        # 应急清单：仅应急任务建立条目，随应急任务阶段回写
        emergency = self._emergency_snapshot(vid)
        if node.get("任务类别") == "应急" and emergency is None:
            row = {"id": row_id}
            row.update(common)
            row["办结"] = task_closed
            store.rows("dispatch_emergency").append(row)
        elif emergency is not None:
            emergency.update(common)
            emergency["办结"] = task_closed

        # 轨迹详情：追加每个节点（轨迹回放需要完整轨迹，不止最新一条）
        track = {
            "id": row_id,
            "车辆id": vid,
            "车辆编号": vehicle["车辆编号"],
            "驾驶员": node.get("驾驶员"),
            "桩号": node.get("桩号"),
            "里程": node.get("里程"),
            "阶段": stage,
            "version": vehicle["version"],
            "时间": node["时间"],
        }
        store.rows("dispatch_track").append(track)

    def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """占用车辆 + 推进首节点：同一临界区内原子成功。

        并发调度只认可先取得版本号的任务：expected_version 与当前不符即冲突，
        失败方既不占用车辆，也不会影响他人占用。
        """
        vehicle_id = int(payload.get("vehicle_id") or 0)
        vehicle = self._require_vehicle_id_or_raise(vehicle_id)
        expected = payload.get("expected_version")
        if expected is None:
            raise DomainError("调度必须携带读取时的版本号 expected_version")
        if int(expected) != int(vehicle.get("version", 0)):
            raise ConflictError(
                f"车辆 {vehicle['车辆编号']} 版本号已变化（当前 v{vehicle.get('version', 0)}），"
                "已被先取得版本号的调度占用")

        head = self._vehicle_view(vehicle, self._timelines().get(int(vehicle["id"])))
        if head["occupied"]:
            raise ConflictError(f"车辆 {vehicle['车辆编号']} 已被占用：{head['stage']} / {head.get('驾驶员')}")
        if head["stage"] == STAGE_MAINTAIN:
            raise DomainError(f"车辆 {vehicle['车辆编号']} 维修中，暂不可派车")

        driver = self._require_text(payload, "driver", "驾驶员")
        task_name = self._require_text(payload, "task_name", "任务名称")
        task_kind = str(payload.get("task_kind") or "排班").strip()
        if task_kind not in DISPATCH_KINDS:
            raise DomainError(f"任务类别只能是 {'/'.join(sorted(DISPATCH_KINDS))}")
        stake = self._require_text(payload, "stake", "起始桩号")
        mileage = self._require_mileage(payload, vehicle)

        # 占用与首节点在同一把锁内同时落库：不存在“占用成功但节点缺失”的中间态
        node = self._append_node(
            vehicle, kind=NODE_DISPATCH, stage=STAGE_DISPATCHED, payload=payload,
            driver=driver, task_name=task_name, task_kind=task_kind,
            stake=stake, mileage=mileage)
        return self._result_view(vehicle, node)

    def _advance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """推进到下一节点；必须出示占用令牌与版本号，他人无法推进或释放。"""
        vehicle_id = int(payload.get("vehicle_id") or 0)
        vehicle = self._require_vehicle_id_or_raise(vehicle_id)
        token = self._require_text(payload, "token", "占用令牌")
        expected = payload.get("expected_version")
        if expected is None:
            raise DomainError("推进节点必须携带 expected_version")

        assembled = self._timelines().get(int(vehicle["id"]))
        last = assembled["nodes"][-1] if assembled and assembled["nodes"] else None
        if last is None or last.get("阶段") not in OCCUPIED_STAGES:
            raise DomainError(f"车辆 {vehicle['车辆编号']} 当前不在可推进阶段")
        if last.get("占用令牌") != token:
            # 未成功取得占用的任务不能推进他人的时序
            raise ConflictError("占用令牌不匹配：不能推进他人占用的车辆")
        if int(expected) != int(vehicle.get("version", 0)):
            raise ConflictError(
                f"版本号已过期（当前 v{vehicle.get('version', 0)}），请刷新时序后重试")

        target = str(payload.get("stage") or "").strip()
        closing = str(payload.get("closing") or "").strip()
        current_index = int(last.get("阶段序号", -1))
        if target:
            if target not in NODE_STAGES:
                raise DomainError(f"目标阶段「{target}」不在推进序列中")
            target_index = NODE_STAGES.index(target)
            # 中间阶段只允许逐格前进：已派车→赶赴现场→作业阶段→收尾归队
            if target_index <= current_index:
                raise DomainError("节点只能沿时序向前推进，不能回退或重复")
            if target_index - current_index > 1:
                raise DomainError(
                    f"不能从「{NODE_STAGES[current_index]}」直接跳到「{target}」，"
                    "请逐节点推进")
            kind = NODE_ADVANCE
            mileage = self._require_mileage(payload, vehicle, optional=True)
        elif closing:
            if closing not in (NODE_RETURN, NODE_REPAIR):
                raise DomainError("收尾动作只能是收车或送修")
            target = STAGE_RETURNED if closing == NODE_RETURN else STAGE_MAINTAIN
            kind = NODE_RETURN if closing == NODE_RETURN else NODE_REPAIR
            mileage = self._require_mileage(payload, vehicle, optional=True)
        else:
            raise DomainError("推进需指定下一阶段 stage，或 closing=收车/送修")

        node = self._append_node(
            vehicle, kind=kind, stage=target, payload=payload,
            stake=(payload.get("stake") or None), mileage=mileage)
        return self._result_view(vehicle, node)

    def repair_done(self, vehicle_id: int) -> dict[str, Any]:
        """维修完成 → 回到在库待命；登记一个修复节点便于审计。"""
        self.boot()
        with self._lock:
            vehicle = self._require_vehicle_id_or_raise(vehicle_id)
            head = self._vehicle_view(vehicle, self._timelines().get(vehicle_id))
            if head["stage"] != STAGE_MAINTAIN:
                raise DomainError(f"车辆 {vehicle['车辆编号']} 当前不在维修中")
            node = self._append_node(
                vehicle, kind=NODE_REPAIRED, stage=STAGE_AVAILABLE,
                payload={"message_id": ""})
            return self._result_view(vehicle, node)

    def _release(self, payload: dict[str, Any]) -> dict[str, Any]:
        """释放占用：仅持有者凭令牌 + 版本号可释放，且以收车节点推进收尾。

        未成功取得占用的任务调用释放会被拒绝，不会释放他人的占用。
        """
        payload = dict(payload)
        payload["closing"] = NODE_RETURN
        payload.setdefault("stage", "")
        return self._advance(payload)

    @staticmethod
    def _require_vehicle_id_or_raise(vehicle_id: int) -> dict[str, Any]:
        if not vehicle_id:
            raise DomainError("vehicle_id 不能为空")
        vehicle = store.find("dispatch_vehicle", vehicle_id)
        if vehicle is None:
            raise DomainError(f"车辆 {vehicle_id} 不存在")
        return vehicle

    @staticmethod
    def _require_mileage(payload: dict[str, Any], vehicle: dict[str, Any],
                         *, optional: bool = False) -> int | None:
        raw = payload.get("mileage")
        if raw in (None, ""):
            if optional:
                return None
            raise DomainError("里程不能为空")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise DomainError(f"里程必须是整数公里，收到：{raw!r}")
        if value < int(vehicle.get("基准里程", 0)):
            raise DomainError(f"里程不能低于车辆基准里程 {vehicle['基准里程']}")
        return value

    def _result_view(self, vehicle: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        view = self.get_timeline(int(vehicle["id"]))
        assert view is not None
        head = view["vehicle"]
        head["applied_node_id"] = node["id"]
        return head

    # ------------------------------------------------------------- 转换前比对
    def comparison(self) -> dict[str, Any]:
        """转换前逐车比对：排班台账/应急清单/轨迹回放三处阶段差异。"""
        self.boot()
        with self._lock:
            vehicles = store.rows("dispatch_vehicle")
            rows: list[dict[str, Any]] = []
            broken = 0
            for vehicle in vehicles:
                code = str(vehicle["车辆编号"])
                schedule = next((r for r in store.rows("dispatch_legacy_schedule")
                                 if r["车辆编号"] == code), None)
                emergency = next((r for r in store.rows("dispatch_legacy_emergency")
                                  if r["车辆编号"] == code), None)
                track = next((r for r in store.rows("dispatch_legacy_track")
                              if r["车辆编号"] == code), None)
                stages = {
                    "排班台账": schedule["台账阶段"] if schedule else "无记录",
                    "应急清单": emergency["应急清单阶段"] if emergency else "无记录",
                    "轨迹回放": track["轨迹阶段"] if track else "无记录",
                }
                actual = {v for v in stages.values() if v != "无记录"}
                consistent = len(actual) <= 1
                if not consistent:
                    broken += 1
                rows.append({
                    "车辆id": vehicle["id"],
                    "车辆编号": code,
                    "车牌号": vehicle["车牌号"],
                    "原驾驶员": schedule["驾驶员"] if schedule else (track["驾驶员"] if track else None),
                    "任务名称": schedule["任务名称"] if schedule else (track["任务名称"] if track else None),
                    "任务类别": schedule["任务类别"] if schedule else None,
                    "未结": bool(schedule and schedule["未结"]),
                    "stages": stages,
                    "consistent": consistent,
                })
            return {
                "migrated": self.is_migrated(),
                "total": len(rows),
                "broken": broken,
                "rows": rows,
            }

    def migrate(self) -> dict[str, Any]:
        """执行转换：历史出勤按原驾驶员+车辆快照保留，未结任务迁入新时序。

        幂等：已迁移则直接返回；全程持锁，比对与迁移在同一口径下完成。
        """
        self.boot()
        with self._lock:
            meta = self._meta()
            if meta.get("migrated"):
                return {"ok": False, "message": "迁移已执行过，时序为唯一事实来源，不可重复迁移"}

            report = self.comparison()
            attendance = store.rows("dispatch_attendance")
            migrated_vehicles: list[str] = []
            frozen = 0

            # 1) 历史出勤冻结：原驾驶员 + 原车辆快照，复制后不再随时序变化
            for old in store.rows("dispatch_legacy_attendance"):
                attendance.append({
                    "id": _new_id(attendance),
                    "车辆编号": old["车辆编号"],
                    "驾驶员": old["驾驶员"],
                    "任务名称": old["任务名称"],
                    "出勤日期": old["出勤日期"],
                    "收车里程": old["收车里程"],
                    "来源": "历史快照",
                    "冻结时间": _now(),
                })
                frozen += 1

            # 2) 未结任务逐车迁入：以原驾驶员与车辆建立时序首节点
            for item in report["rows"]:
                if not item["未结"]:
                    continue
                vehicle = store.find("dispatch_vehicle", item["车辆id"])
                assert vehicle is not None
                legacy_track = next((r for r in store.rows("dispatch_legacy_track")
                                     if r["车辆编号"] == item["车辆编号"]), None)
                mileage = int(legacy_track["最新里程"]) if legacy_track else int(vehicle["基准里程"])
                stake = legacy_track["最新桩号"] if legacy_track else "库区"
                self._append_node(
                    vehicle, kind=NODE_DISPATCH, stage=STAGE_DISPATCHED,
                    payload={"message_id": f"migrate-{vehicle['车辆编号']}"},
                    driver=item["原驾驶员"], task_name=item["任务名称"],
                    task_kind=item["任务类别"] or "排班",
                    stake=stake, mileage=mileage)
                migrated_vehicles.append(str(vehicle["车辆编号"]))

            meta["migrated"] = True
            meta["migrated_at"] = _now()
            return {
                "ok": True,
                "message": (f"迁移完成：{len(migrated_vehicles)} 辆在途车迁入新时序，"
                            f"冻结 {frozen} 条历史出勤快照"),
                "migrated_vehicles": migrated_vehicles,
                "frozen_attendance": frozen,
                "broken_before": report["broken"],
            }


service = DispatchService()
