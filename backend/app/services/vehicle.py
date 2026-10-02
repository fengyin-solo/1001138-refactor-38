"""养护车辆业务规则：车辆档案维护与调度阶段回写。

重构后「派车出车 / 收车归库」不再在本模块直接改状态，统一走调度时序
（app.services.dispatch）；车辆列表里看到的调度阶段、占用标记与版本号
全部回写自时序，保证排班台账、应急清单、轨迹详情同口径。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "vehicle"
REQUIRED_FIELDS = ["车辆编号", "车辆类型", "车牌号"]
STATUS_ORDER = ["在库", "出车作业", "维修", "报废"]
# 仅保留档案级动作；派车/收车已迁入调度时序
ACTION_RULES = {"送修车辆": "维修", "维修完成": "在库", "车辆报废": "报废"}
NEGATIVE_ACTIONS = []

# 已迁走的旧动作：给出明确指引，避免调度员在错误页面继续制造第二套状态
DELEGATED_ACTIONS = {"派车出车", "收车归库"}


class VehicleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 延迟导入，避免档案模块与调度模块循环初始化
        from app.services.dispatch import dispatch_service

        dispatch_rows = {item["vehicle_code"]: item for item in dispatch_service.list_vehicles()}
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("车辆编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]

        annotated: list[dict[str, Any]] = []
        for row in rows:
            view = dict(row)
            timeline = dispatch_rows.get(str(view.get("车辆编号", "")))
            if timeline is not None and dispatch_service.is_migrated():
                # 调度阶段回写排班台账：同一辆车只有时序这一个事实来源
                view["调度阶段"] = timeline["stage"]
                view["是否占用"] = "是" if timeline["occupied"] else "否"
                view["调度版本"] = timeline["version"]
                view["当前里程"] = timeline["odometer"]
                holder = timeline["occupier"] or {}
                view["占用任务"] = holder.get("task", "—")
            else:
                view["调度阶段"] = "待迁移"
                view["是否占用"] = "否"
                view["调度版本"] = 0
                view["占用任务"] = "—"
            annotated.append(view)

        total = len(annotated)
        start = max(page - 1, 0) * size
        return annotated[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for optional in ("所属单位", "年检日期", "驾驶员", "当前里程"):
            if values.get(optional) is not None:
                entry[optional] = values[optional]
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        from app.services.dispatch import dispatch_service

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"养护车辆 {entry_id} 不存在或已归档"
        if action in DELEGATED_ACTIONS:
            return None, (f"「{action}」已统一到车辆调度时序，请在「调度时序图」页面"
                          f"凭版本号占用/推进/释放车辆")
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于养护车辆可执行范围"

        code = str(entry.get("车辆编号", ""))
        timeline = next((v for v in dispatch_service.list_vehicles()
                         if v["vehicle_code"] == code), None)
        if action == "送修车辆" and timeline is not None and timeline["occupied"]:
            return None, (f"车辆 {code} 正被任务「{(timeline['occupier'] or {}).get('task', '')}」"
                          f"占用（v{timeline['version']}），须先收车归库再送修")

        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 档案状态（维修/在库/报废）联动到时序：占用中的车辆上面已拦截，到这里必空闲
        dispatch_service.set_registry_stage(code, "维修" if target == "维修" else "待命")
        return entry, f"养护车辆已{action}"
