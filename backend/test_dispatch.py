"""调度时序域场景测试（标准库断言，无需 pytest）。"""
import sys
import threading
import time

sys.path.insert(0, '.')

from app.services.dispatch import (  # noqa: E402
    service, ConflictError, DomainError,
    STAGE_DISPATCHED, STAGE_ENROUTE, STAGE_ONSITE,
)

# 1) 迁移：历史快照 + 未结任务入时序
res = service.migrate()
assert res["frozen_attendance"] == 2
assert service.migrate()["ok"] is False
assert len(service.list_attendance()) == 2
att = service.list_attendance()[0]
assert att["来源"] == "历史快照" and att["驾驶员"] == "王建国"

tl = {v["车辆编号"]: v for v in service.list_timelines()}
assert not tl["VEHI-1004"]["occupied"]
for code in ("VEHI-1001", "VEHI-1002", "VEHI-1003"):
    assert tl[code]["stage"] == STAGE_DISPATCHED and tl[code]["occupied"]
assert tl["VEHI-1001"]["驾驶员"] == "王建国"

# 2) 连续节点推进
v1 = tl["VEHI-1001"]
service.apply("advance", {"vehicle_id": 1, "token": v1["占用令牌"],
    "expected_version": v1["version"], "stage": STAGE_ENROUTE,
    "stake": "K12+050", "mileage": 52160}, "a1")
v1b = service.get_timeline(1)["vehicle"]
assert v1b["version"] == v1["version"] + 1

# 3) 他人令牌不能推进
try:
    service.apply("advance", {"vehicle_id": 1, "token": "forged",
        "expected_version": v1b["version"], "stage": STAGE_ONSITE}, "ax")
    raise AssertionError("应冲突")
except ConflictError as e:
    assert "令牌" in str(e)

# 4) 过期版本号被拒
try:
    service.apply("advance", {"vehicle_id": 1, "token": v1["占用令牌"],
        "expected_version": v1["version"], "stage": STAGE_ONSITE,
        "stake": "K12+300", "mileage": 52180}, "astale")
    raise AssertionError("旧版本应失败")
except ConflictError:
    pass

# 5) 合法推进
out = service.apply("advance", {"vehicle_id": 1, "token": v1["占用令牌"],
    "expected_version": v1b["version"], "stage": STAGE_ONSITE,
    "stake": "K12+300", "mileage": 52180}, "a2")
assert out["result"]["stage"] == STAGE_ONSITE and out["result"]["version"] == 3

# 6) 节点不能回退
try:
    service.apply("advance", {"vehicle_id": 1, "token": v1["占用令牌"],
        "expected_version": 3, "stage": STAGE_ENROUTE}, "aback")
    raise AssertionError("回退应失败")
except DomainError:
    pass

# 7) 消息去重
dup = service.apply("advance", {"vehicle_id": 1, "token": v1["占用令牌"],
    "expected_version": v1b["version"], "stage": STAGE_ONSITE}, "a2")
assert dup["duplicate"] is True
n_after = len(service.get_timeline(1)["nodes"])
service.apply("advance", {"vehicle_id": 1, "token": v1["占用令牌"],
    "expected_version": v1b["version"], "stage": STAGE_ONSITE}, "a2")
assert len(service.get_timeline(1)["nodes"]) == n_after

# 8) 三处回写实时一致
sched = {r["车辆编号"]: r for r in service.list_schedule()}
em = {r["车辆编号"]: r for r in service.list_emergency()}
tr = {r["车辆编号"]: r for r in service.list_tracks()}
assert sched["VEHI-1001"]["阶段"] == STAGE_ONSITE
assert tr["VEHI-1001"]["阶段"] == STAGE_ONSITE
assert tr["VEHI-1001"]["桩号"] == "K12+300"
assert tr["VEHI-1001"]["里程"] == 52180
assert em["VEHI-1003"]["阶段"] == STAGE_DISPATCHED
assert "VEHI-1001" not in em

# 9) 并发调度：只认可先取得版本号者
results = {}
barrier = threading.Barrier(2)


def worker(name, mid):
    barrier.wait()
    try:
        service.apply("dispatch", {"vehicle_id": 4, "expected_version": 0,
            "driver": name, "task_name": "并发-" + name, "task_kind": "排班",
            "stake": "K1+000", "mileage": 31850}, mid)
        results[name] = "ok"
    except ConflictError:
        results[name] = "conflict"


threading.Thread(target=worker, args=("甲", "ma")).start()
threading.Thread(target=worker, args=("乙", "mb")).start()
time.sleep(0.5)
assert sorted(results.values()) == ["conflict", "ok"], results
v4 = service.get_timeline(4)["vehicle"]
assert v4["version"] == 1 and v4["occupied"]
assert results[v4["驾驶员"]] == "ok"

# 10) 失败方不能释放他人占用
try:
    service.apply("release", {"vehicle_id": 4, "expected_version": 1,
        "token": "forged"}, "mrx")
    raise AssertionError("伪造令牌应收敛为冲突")
except ConflictError:
    pass
v4s = service.get_timeline(4)["vehicle"]
assert v4s["occupied"] and v4s["version"] == 1

# 11) 持有者收车
service.apply("release", {"vehicle_id": 4, "expected_version": 1,
    "token": v4["占用令牌"], "stake": "库区", "mileage": 31900}, "mret")
v4b = service.get_timeline(4)["vehicle"]
assert v4b["stage"] == "收车归库" and not v4b["occupied"]
s4 = next(r for r in service.list_schedule() if r["车辆编号"] == "VEHI-1004")
assert s4["办结"] and s4["阶段"] == "收车归库"
t4 = next(r for r in service.list_tracks() if r["车辆编号"] == "VEHI-1004")
assert t4["阶段"] == "收车归库" and t4["里程"] == 31900

# 12) 失败的调度不留占用（原子性）
try:
    service.apply("dispatch", {"vehicle_id": 4, "expected_version": v4b["version"],
        "driver": "", "task_name": "", "stake": "", "mileage": 1}, "mbad")
    raise AssertionError("应被拒绝")
except DomainError:
    pass
v4c = service.get_timeline(4)["vehicle"]
assert v4c["version"] == v4b["version"] and v4c["stage"] == "收车归库"

# 13) 里程低于基准拒绝
try:
    service.apply("dispatch", {"vehicle_id": 4, "expected_version": v4b["version"],
        "driver": "丙", "task_name": "x", "task_kind": "排班",
        "stake": "K1", "mileage": 1}, "mmile")
    raise AssertionError("里程校验应失败")
except DomainError:
    pass

# 14) 收车后再派应急任务 -> 进入应急清单
v2 = service.get_timeline(2)["vehicle"]
service.apply("advance", {"vehicle_id": 2, "token": v2["占用令牌"],
    "expected_version": v2["version"], "closing": "收车",
    "stake": "库区", "mileage": 48760}, "w2ret")
v2b = service.get_timeline(2)["vehicle"]
service.apply("dispatch", {"vehicle_id": 2, "expected_version": v2b["version"],
    "driver": "李秀兰", "task_name": "夜间应急除雪", "task_kind": "应急",
    "stake": "G318 K29", "mileage": 48800}, "wem")
em2 = {r["车辆编号"]: r for r in service.list_emergency()}
assert "VEHI-1002" in em2
assert em2["VEHI-1002"]["阶段"] == "已派车"
assert not em2["VEHI-1002"]["办结"]

# 15) 消息日志覆盖 applied / conflict / rejected
statuses = {r["status"] for r in service.list_messages()}
assert {"applied", "conflict", "rejected"} <= statuses

# 16) 送修 -> 维修中 -> 修复待命
v2c = service.get_timeline(2)["vehicle"]
service.apply("advance", {"vehicle_id": 2, "token": v2c["占用令牌"],
    "expected_version": v2c["version"], "closing": "送修",
    "stake": "维修车间", "mileage": 48810}, "wrep")
v2d = service.get_timeline(2)["vehicle"]
assert v2d["stage"] == "维修中" and not v2d["occupied"]
try:
    service.apply("dispatch", {"vehicle_id": 2, "expected_version": v2d["version"],
        "driver": "x", "task_name": "y", "task_kind": "排班",
        "stake": "K1", "mileage": 48900}, "wbusy")
    raise AssertionError("维修中不可派车")
except DomainError:
    pass
service.apply("repair_done", {"vehicle_id": 2}, "wok")
v2e = service.get_timeline(2)["vehicle"]
assert v2e["stage"] == "在库待命" and not v2e["occupied"]

print("全部场景通过 ✔")
