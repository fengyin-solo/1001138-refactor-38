"""车辆调度时序图接口。

所有写操作走统一命令入口 /api/dispatch/commands：消息携带 message_id 去重，
并发冲突返回 409，业务规则不满足返回 422；路由层不做业务判断。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.services.dispatch import ConflictError, DomainError, service

router = APIRouter(prefix="/api/dispatch", tags=["车辆调度时序"])


class CommandPayload(BaseModel):
    command: str = Field(description="dispatch / advance / release / repair_done")
    message_id: str | None = Field(default=None, description="命令消息唯一编号，用于幂等去重")
    vehicle_id: int | None = None
    expected_version: int | None = Field(default=None, description="读取时序时拿到的版本号（CAS）")
    token: str | None = Field(default=None, description="占用令牌，推进/释放时必填")
    driver: str | None = None
    task_name: str | None = None
    task_kind: str | None = Field(default=None, description="排班 / 应急")
    stake: str | None = None
    mileage: int | None = None
    stage: str | None = None
    closing: str | None = Field(default=None, description="收车 / 送修")


def _fail(status_code: int, message: str, *, duplicate: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "message": message, "duplicate": duplicate},
    )


@router.get("/timelines")
def list_timelines(keyword: str | None = None,
                   only_occupied: bool = False) -> dict[str, Any]:
    """调度时序图：车辆列表 + 当前阶段 + 版本号 + 占用令牌归属。"""
    items = service.list_timelines(keyword=keyword, only_occupied=only_occupied)
    return {"items": items, "total": len(items), "migrated": service.is_migrated()}


@router.get("/timelines/{vehicle_id}")
def get_timeline(vehicle_id: int) -> dict[str, Any]:
    """单车时序详情：连续节点 + 排班/应急/轨迹三处回写。"""
    timeline = service.get_timeline(vehicle_id)
    if timeline is None:
        return _fail(404, f"车辆 {vehicle_id} 不存在")
    return timeline


@router.post("/commands")
def run_command(payload: CommandPayload) -> dict[str, Any]:
    """统一命令入口：先按 message_id 去重，再在锁内原子应用。"""
    values = payload.model_dump(exclude_none=True)
    command = values.pop("command")
    message_id = values.pop("message_id", None)
    try:
        outcome = service.apply(command, values, message_id)
    except ConflictError as exc:
        return _fail(409, str(exc))
    except DomainError as exc:
        return _fail(422, str(exc))
    if outcome["duplicate"]:
        return {"ok": True, "duplicate": True,
                "message": "消息已处理，返回首次应用结果（已去重）",
                "message_log": outcome["message"], "result": None}
    return {"ok": True, "duplicate": False,
            "message": "命令已应用，阶段已同步回写三处台账",
            "result": outcome["result"]}


@router.get("/schedule")
def schedule_ledger() -> dict[str, Any]:
    """排班台账（阶段由时序回写）。"""
    items = service.list_schedule()
    return {"items": items, "total": len(items)}


@router.get("/emergency")
def emergency_list() -> dict[str, Any]:
    """应急清单（阶段由时序回写）。"""
    items = service.list_emergency()
    return {"items": items, "total": len(items)}


@router.get("/tracks")
def track_details() -> dict[str, Any]:
    """轨迹详情（每车最新节点，阶段/桩号/里程由时序回写）。"""
    items = service.list_tracks()
    return {"items": items, "total": len(items)}


@router.get("/attendance")
def attendance_history() -> dict[str, Any]:
    """历史出勤（原驾驶员+原车辆的冻结快照）。"""
    items = service.list_attendance()
    return {"items": items, "total": len(items)}


@router.get("/messages")
def message_log() -> dict[str, Any]:
    """命令消息去重与应用日志。"""
    items = service.list_messages()
    return {"items": items, "total": len(items)}


@router.get("/migration/comparison")
def migration_comparison() -> dict[str, Any]:
    """转换前逐车比对：三处页面前迁移阶段差异。"""
    return service.comparison()


@router.post("/migration/run")
def migration_run() -> dict[str, Any]:
    """执行转换：历史出勤冻结快照，未结任务迁入新时序。"""
    return service.migrate()
