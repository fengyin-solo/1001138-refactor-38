"""车辆调度时序接口。

唯一事实来源是 /api/dispatch/timeline 下的车辆时序；排班台账、应急清单、
轨迹详情都是它的只读投影，三页阶段因此不可能再互相打架。

写接口统一要求 message_id（去重）与 version（乐观锁），占用/推进/释放
要么整体成功（版本号 +1），要么 409 失败且保持他人占用不变。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload
from app.services.dispatch import ConflictError, dispatch_service

router = APIRouter(prefix="/api/dispatch", tags=["车辆调度时序"])


def _fail(message: str, status_code: int = 409) -> JSONResponse:
    """并发冲突类错误走 409，便于前端提示「刷新版本后重试」。"""
    return JSONResponse(status_code=status_code, content={"ok": False, "message": message})


@router.get("/vehicles")
def list_vehicles() -> dict[str, object]:
    """车辆调度总览：阶段、占用者、版本号与连续节点一览。"""
    items = dispatch_service.list_vehicles()
    return {"items": items, "total": len(items)}


@router.get("/vehicles/{code}")
def get_vehicle(code: str) -> dict[str, object]:
    try:
        return dispatch_service.get_vehicle(code)
    except ConflictError as exc:
        return _fail(str(exc), 404)


@router.post("/occupy", response_model=ActionResult)
def occupy(payload: EntryPayload) -> ActionResult | JSONResponse:
    """占用车辆并挂驾驶员/任务首节点（原子提交）。"""
    try:
        snapshot = dispatch_service.occupy(payload.values)
    except ConflictError as exc:
        return _fail(str(exc))
    return ActionResult(ok=True,
                        message=f"车辆 {snapshot['vehicle_code']} 已占用（v{snapshot['version']}）",
                        entry=snapshot)


@router.post("/advance", response_model=ActionResult)
def advance(payload: EntryPayload) -> ActionResult | JSONResponse:
    """推进时序节点：已派车→已出车（里程）→到场作业（桩号）。"""
    try:
        snapshot = dispatch_service.advance(payload.values)
    except ConflictError as exc:
        return _fail(str(exc))
    return ActionResult(ok=True,
                        message=f"车辆 {snapshot['vehicle_code']} 已推进至{snapshot['stage']}"
                                f"（v{snapshot['version']}）",
                        entry=snapshot)


@router.post("/takeover", response_model=ActionResult)
def takeover(payload: EntryPayload) -> ActionResult | JSONResponse:
    """接管迁移进来的未结任务占用（仅「历史迁移」挂名占用可被接管）。"""
    try:
        snapshot = dispatch_service.takeover(payload.values)
    except ConflictError as exc:
        return _fail(str(exc))
    return ActionResult(ok=True,
                        message=f"已接管车辆 {snapshot['vehicle_code']} 的未结任务"
                                f"（v{snapshot['version']}）",
                        entry=snapshot)


@router.post("/release", response_model=ActionResult)
def release(payload: EntryPayload) -> ActionResult | JSONResponse:
    """收车归库 / 撤销派车：仅占用者本人持正确版本号可释放。"""
    try:
        snapshot = dispatch_service.release(payload.values)
    except ConflictError as exc:
        return _fail(str(exc))
    return ActionResult(ok=True,
                        message=f"车辆 {snapshot['vehicle_code']} 已释放，当前{snapshot['stage']}"
                                f"（v{snapshot['version']}）",
                        entry=snapshot)


@router.get("/migration/preview")
def migration_preview() -> dict[str, object]:
    """转换前逐车比对排班表 / 轨迹回放 / 应急汇总三处阶段。"""
    return dispatch_service.migration_preview()


@router.post("/migration/run", response_model=ActionResult)
def migration_run(payload: EntryPayload | None = None) -> ActionResult:
    """执行转换：历史出勤快照归档，未结任务迁入时序（幂等）。"""
    values = payload.values if payload is not None else {}
    report = dispatch_service.migrate(values)
    return ActionResult(ok=True, message=report["message"], entry=report)


@router.get("/migration/history")
def migration_history(vehicle_code: str | None = None) -> dict[str, object]:
    """历史出勤：按原驾驶员与车辆快照保留，只读。"""
    return dispatch_service.history(vehicle_code)


@router.get("/schedule-ledger")
def schedule_ledger() -> dict[str, object]:
    """排班台账（时序投影）：阶段由时序回写。"""
    return dispatch_service.schedule_ledger()


@router.get("/emergency")
def emergency_list() -> dict[str, object]:
    """应急清单（时序投影）：未结应急任务与其实时阶段。"""
    return dispatch_service.emergency_list()


@router.get("/tracks")
def track_detail(vehicle_code: str | None = None) -> dict[str, object]:
    """轨迹详情（时序投影）：驾驶员/任务/桩号/里程连续节点。"""
    return dispatch_service.track_detail(vehicle_code)
