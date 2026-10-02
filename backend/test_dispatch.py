"""调度时序领域逻辑的离线验证（不依赖 FastAPI）。

覆盖：转换前逐车比对、历史快照/未结任务迁移、占用+首节点原子性、
消息去重、版本号 CAS、不能释放他人占用、三页投影一致性。
"""
from __future__ import annotations

import sys
import threading

from app.services.dispatch import (
    NODE_MILEAGE,
    NODE_STAKE,
    STAGE_ASSIGNED,
    STAGE_DEPARTED,
    STAGE_ONSITE,
    STAGE_STANDBY,
    ConflictError,
    DispatchService,
    new_message_id,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(f"{label} {detail}")


def main() -> int:
    svc = DispatchService()

    # ---------- 1. 转换前逐车比对 ----------
    preview = svc.migration_preview()
    check("迁移前标记为未迁移", preview["migrated"] is False)
    check("共比对 3 辆车", preview["vehicles_checked"] == 3, str(preview["vehicles_checked"]))
    # VEHI-0001 排班=已派车 / 轨迹=行驶中，映射后不一致；VEHI-0002 已出车 vs 作业中
    check("检出 2 辆分页阶段冲突", preview["conflict_count"] == 2,
          str([c["vehicle_code"] for c in preview["conflicts"]]))
    check("未结应急任务识别为 1 条", preview["open_task_count"] == 1)
    v1 = next(v for v in preview["per_vehicle"] if v["vehicle_code"] == "VEHI-0001")
    check("冲突车收敛到最靠后阶段(已出车)", v1["收敛阶段"] == STAGE_DEPARTED, str(v1["sources"]))
    v2 = next(v for v in preview["per_vehicle"] if v["vehicle_code"] == "VEHI-0002")
    check("VEHI-0002 收敛到到场作业", v2["收敛阶段"] == STAGE_ONSITE)

    # ---------- 2. 迁移 ----------
    report = svc.migrate({})
    check("迁移成功", report["migrated"] is True, report.get("message", ""))
    check("历史出勤 3 条全部快照归档", len(report["history_snapshots"]) == 3)
    snap = report["history_snapshots"][0]
    check("历史快照保留原驾驶员", bool(snap["driver_snapshot"]))
    check("历史快照保留车辆快照", snap["vehicle_snapshot"]["车辆编号"] == snap["vehicle_code"])
    check("未结任务 1 条迁入时序", len(report["moved_open_tasks"]) == 1,
          str(report["moved_open_tasks"]))
    moved = report["moved_open_tasks"][0]
    check("迁入任务车辆为 VEHI-0002", moved["vehicle_code"] == "VEHI-0002")
    check("迁入任务停在到场作业", moved["stage"] == STAGE_ONSITE)

    v2cur = svc.get_vehicle("VEHI-0002")
    check("迁入车被占用", v2cur["occupied"] is True)
    check("迁入节点序列为 驾驶员→任务→里程→桩号",
          [n["type"] for n in v2cur["timeline"]] == ["驾驶员", "任务", "里程", "桩号"],
          str([n["type"] for n in v2cur["timeline"]]))
    v3cur = svc.get_vehicle("VEHI-0003")
    check("已完结出勤车辆回待命", v3cur["stage"] == STAGE_STANDBY and not v3cur["occupied"])

    # 迁移幂等
    again = svc.migrate({"message_id": "rerun-1"})
    check("重复迁移幂等不重复建任务", again.get("idempotent") is True)
    check("幂等后 VEHI-0002 仍只有 1 个任务",
          len(svc.get_vehicle("VEHI-0002")["tasks"]) == 1)

    # ---------- 3. 三页投影一致性 ----------
    ledger = {r["车辆编号"]: r for r in svc.schedule_ledger()["items"]}
    emergency = {r["车辆编号"]: r for r in svc.emergency_list()["items"]}
    tracks = {r["车辆编号"]: r for r in svc.track_detail()["items"]}
    check("台账 VEHI-0002 阶段=到场作业", ledger["VEHI-0002"]["排班阶段"] == STAGE_ONSITE)
    check("应急清单 VEHI-0002 阶段=到场作业", emergency["VEHI-0002"]["应急阶段"] == STAGE_ONSITE)
    check("轨迹 VEHI-0002 阶段=到场作业", tracks["VEHI-0002"]["轨迹阶段"] == STAGE_ONSITE)
    check("应急清单只有 1 条未结", len(emergency) == 1)

    # ---------- 4. 占用：缺版本/缺消息/重复消息 ----------
    v3 = svc.get_vehicle("VEHI-0003")
    base_version = v3["version"]
    try:
        svc.occupy({"vehicle_code": "VEHI-0003", "driver": "甲", "task": "T",
                    "message_id": new_message_id()})
        check("占用缺版本号被拒绝", False)
    except ConflictError:
        check("占用缺版本号被拒绝", True)

    mid = new_message_id()
    r = svc.occupy({"vehicle_code": "VEHI-0003", "driver": "周一鸣", "task": "夜间标线补划",
                    "stake": "K5+200", "operator": "调度A", "message_id": mid,
                    "version": base_version})
    check("占用成功版本号 +1", r["version"] == base_version + 1)
    check("占用原子挂接驾驶员+任务两个首节点",
          [n["type"] for n in r["timeline"]] == ["驾驶员", "任务"])
    check("占用成功车辆进入已派车", r["stage"] == STAGE_ASSIGNED and r["occupied"])

    # 重复消息：不能重复应用
    try:
        svc.occupy({"vehicle_code": "VEHI-0003", "driver": "周一鸣", "task": "夜间标线补划",
                    "operator": "调度A", "message_id": mid, "version": r["version"]})
        check("重复 message_id 被丢弃", False)
    except ConflictError:
        check("重复 message_id 被丢弃", True)
    check("去重拒绝后版本不变", svc.get_vehicle("VEHI-0003")["version"] == r["version"])
    check("去重拒绝后节点不增加", len(svc.get_vehicle("VEHI-0003")["timeline"]) == 2)

    # ---------- 5. 并发：过期版本占用失败 ----------
    try:
        svc.occupy({"vehicle_code": "VEHI-0003", "driver": "钱二", "task": "抢车任务",
                    "operator": "调度B", "message_id": new_message_id(),
                    "version": base_version})  # 旧版本
        check("过期版本并发占用失败", False)
    except ConflictError as exc:
        check("过期版本并发占用失败", True)
        check("失败提示保留他人占用", "占用" in str(exc))

    # 同一时刻另一个调度对另一辆车占用成功（先取得版本者胜）
    v1cur = svc.get_vehicle("VEHI-0001")
    other = svc.occupy({"vehicle_code": "VEHI-0001", "driver": "孙三", "task": "护栏维修",
                        "stake": "K12+300", "operator": "调度B",
                        "message_id": new_message_id(), "version": v1cur["version"]})
    check("未冲突车辆可正常占用", other["occupied"] is True)

    # ---------- 6. 推进节点 ----------
    cur = svc.get_vehicle("VEHI-0003")
    adv1 = svc.advance({"vehicle_code": "VEHI-0003", "target_stage": STAGE_DEPARTED,
                        "mileage": 96300 + 12.5, "operator": "调度A",
                        "message_id": new_message_id(), "version": cur["version"]})
    check("出车推进成功并追加里程节点",
          adv1["stage"] == STAGE_DEPARTED and adv1["timeline"][-1]["type"] == NODE_MILEAGE)
    try:
        svc.advance({"vehicle_code": "VEHI-0003", "target_stage": STAGE_ONSITE,
                     "stake": "K5+200", "operator": "调度A",
                     "message_id": new_message_id(), "version": cur["version"]})
        check("过期版本推进失败", False)
    except ConflictError:
        check("过期版本推进失败", True)
    cur = svc.get_vehicle("VEHI-0003")
    adv2 = svc.advance({"vehicle_code": "VEHI-0003", "target_stage": STAGE_ONSITE,
                        "stake": "K5+200", "operator": "调度A",
                        "message_id": new_message_id(), "version": cur["version"]})
    check("到场推进成功并追加桩号节点",
          adv2["stage"] == STAGE_ONSITE and adv2["timeline"][-1]["type"] == NODE_STAKE)
    try:
        svc.advance({"vehicle_code": "VEHI-0003", "target_stage": STAGE_DEPARTED,
                     "mileage": 99999, "operator": "调度A",
                     "message_id": new_message_id(), "version": adv2["version"]})
        check("阶段不能回退", False)
    except ConflictError:
        check("阶段不能回退", True)
    try:
        svc.advance({"vehicle_code": "VEHI-0003", "target_stage": STAGE_ONSITE,
                     "stake": "K6", "operator": "调度A",
                     "message_id": new_message_id(), "version": adv2["version"]})
        check("重复停留同阶段被拒绝", False)
    except ConflictError:
        check("重复停留同阶段被拒绝", True)

    # ---------- 7. 释放：占用者校验 ----------
    cur = svc.get_vehicle("VEHI-0003")
    try:
        svc.release({"vehicle_code": "VEHI-0003", "operator": "调度B",
                     "message_id": new_message_id(), "version": cur["version"]})
        check("非占用者不能释放", False)
    except ConflictError:
        check("非占用者不能释放", True)
    cur = svc.get_vehicle("VEHI-0003")
    check("释放失败后占用保持", cur["occupied"] and cur["stage"] == STAGE_ONSITE)
    rel = svc.release({"vehicle_code": "VEHI-0003", "operator": "调度A",
                       "mileage": 96300 + 38.0, "message_id": new_message_id(),
                       "version": cur["version"]})
    check("占用者本人收车成功回待命",
          rel["stage"] == STAGE_STANDBY and rel["occupier"] is None)
    check("收车任务已完成", rel["tasks"][0]["status"] == "已完成")
    check("收车追加里程+任务两个结单节点",
          [n["type"] for n in rel["timeline"][-2:]] == [NODE_MILEAGE, "任务"])

    # 撤销派车
    v1cur = svc.get_vehicle("VEHI-0001")
    cancel = svc.release({"vehicle_code": "VEHI-0001", "operator": "调度B",
                          "cancel": True, "message_id": new_message_id(),
                          "version": v1cur["version"]})
    check("撤销派车回待命且任务取消",
          cancel["stage"] == STAGE_STANDBY
          and cancel["tasks"][0]["status"] == "已取消")

    # 迁移任务挂名「历史迁移」：接管前不能直接推进/释放，接管后可以
    cur = svc.get_vehicle("VEHI-0002")
    try:
        svc.advance({"vehicle_code": "VEHI-0002", "target_stage": STAGE_DEPARTED,
                     "mileage": cur["odometer"] + 5, "operator": "调度A",
                     "message_id": new_message_id(), "version": cur["version"]})
        check("非占用者不能推进迁移任务", False)
    except ConflictError:
        check("非占用者不能推进迁移任务", True)
    cur = svc.get_vehicle("VEHI-0002")
    tk = svc.takeover({"vehicle_code": "VEHI-0002", "operator": "调度A",
                       "message_id": new_message_id(), "version": cur["version"]})
    check("接管历史任务成功", tk["occupier"]["operator"] == "调度A" and tk["version"] == 2)
    # 接管后仍在到场作业，直接收车（无更高阶段可推）
    rel = svc.release({"vehicle_code": "VEHI-0002", "operator": "调度A",
                       "mileage": tk["odometer"] + 20, "message_id": new_message_id(),
                       "version": tk["version"]})
    check("接管后可收车归库", rel["stage"] == STAGE_STANDBY)
    try:
        svc.takeover({"vehicle_code": "VEHI-0002", "operator": "调度B",
                      "message_id": new_message_id(), "version": rel["version"]})
        check("空闲车无可接管占用", False)
    except ConflictError:
        check("空闲车无可接管占用", True)

    # ---------- 8. 真实线程并发：只允许先取得版本者 ----------
    svc2 = DispatchService()
    svc2.migrate({})
    # 迁移后 VEHI-0002 仍被未结应急任务占用，VEHI-0003 空闲可作为并发抢占目标
    target = svc2.get_vehicle("VEHI-0003")
    ver = target["version"]
    check("并发目标初始为待命", target["stage"] == STAGE_STANDBY)
    results: list[str] = []

    def worker(operator: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        try:
            svc2.occupy({"vehicle_code": "VEHI-0003", "driver": operator,
                         "task": f"任务-{operator}", "stake": "K9+000",
                         "operator": operator, "message_id": new_message_id(),
                         "version": ver})
            results.append(f"{operator}:win")
        except ConflictError:
            results.append(f"{operator}:lose")

    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=worker, args=(name, barrier))
               for name in ("调度X", "调度Y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [r for r in results if r.endswith(":win")]
    check("并发占用恰有 1 个胜者", len(wins) == 1, str(results))
    final = svc2.get_vehicle("VEHI-0003")
    check("败者未改动车辆阶段/占用", final["occupier"]["operator"] in ("调度X", "调度Y")
          and final["version"] == ver + 1)
    # 由并发胜者收车后再送修
    winner = final["occupier"]["operator"]
    svc2.release({"vehicle_code": "VEHI-0003", "operator": winner,
                  "mileage": 96350, "message_id": new_message_id(),
                  "version": final["version"]})

    # ---------- 9. 维修档案联动 ----------
    try:
        svc2.set_registry_stage("VEHI-0002", "维修")
        check("占用车辆不能送修", False)
    except ConflictError:
        check("占用车辆不能送修", True)
    svc2.set_registry_stage("VEHI-0003", "维修")
    check("空闲车辆进入维修态", svc2.get_vehicle("VEHI-0003")["stage"] == "维修")
    try:
        cur = svc2.get_vehicle("VEHI-0003")
        svc2.occupy({"vehicle_code": "VEHI-0003", "driver": "甲", "task": "T",
                     "message_id": new_message_id(), "version": cur["version"]})
        check("维修态车辆不可占用", False)
    except ConflictError:
        check("维修态车辆不可占用", True)

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        for f in failures:
            print(" -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
