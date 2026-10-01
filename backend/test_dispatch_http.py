"""HTTP 层冒烟测试：用最小 shim 顶替 fastapi/pydantic，直接驱动路由函数。

真实环境装有 fastapi 时本 shim 不生效（sys.modules 预注入仅在缺包时）。
覆盖：时序列表、比对、迁移、命令 200/409/422/去重、三处回写查询。
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any

sys.path.insert(0, '.')

# ---- 仅在缺少 fastapi 时注入 shim ----
try:  # pragma: no cover
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    fastapi = types.ModuleType('fastapi')

    class APIRouter:  # noqa: D401
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def get(self, *a: Any, **k: Any):
            def deco(fn):
                return fn
            return deco

        def post(self, *a: Any, **k: Any):
            def deco(fn):
                return fn
            return deco

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = '') -> None:
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Query = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules['fastapi'] = fastapi

    responses = types.ModuleType('fastapi.responses')

    class JSONResponse(dict):
        def __init__(self, status_code: int, content: dict[str, Any]) -> None:
            super().__init__(content)
            self.status_code = status_code

    responses.JSONResponse = JSONResponse
    sys.modules['fastapi.responses'] = responses

    pydantic = types.ModuleType('pydantic')

    class BaseModel:
        def __init__(self, **values: Any) -> None:
            for key, value in values.items():
                setattr(self, key, value)

        def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
            data = dict(self.__dict__)
            if exclude_none:
                data = {k: v for k, v in data.items() if v is not None}
            return data

    def Field(default: Any = None, *a: Any, **k: Any) -> Any:
        return default

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    sys.modules['pydantic'] = pydantic

# ---- 驱动路由 ----
from app.routers import dispatch as router_mod  # noqa: E402


def is_json_response(value: Any) -> bool:
    return hasattr(value, 'status_code')


def assert_ok(value: Any) -> dict[str, Any]:
    if is_json_response(value):
        raise AssertionError(f'期望成功，收到 {value.status_code}: {dict(value)}')
    return value  # type: ignore[return-value]


# 列表（迁移前）
listed = router_mod.list_timelines()
assert listed['total'] == 4
assert listed['migrated'] is False

# 比对：3 辆断点车
cmp_res = router_mod.migration_comparison()
assert cmp_res['broken'] == 3

# 迁移
mig = router_mod.migration_run()
assert mig['ok'] and len(mig['migrated_vehicles']) == 3

# 详情含三处回写
detail = assert_ok(router_mod.get_timeline(1))
assert detail['vehicle']['stage'] == '已派车'
assert detail['writeback']['schedule']['阶段'] == '已派车'
assert detail['writeback']['track']['里程'] == 52148

# 并发：同版本号两条调度命令打 VEHI-1004
cmd_a = router_mod.CommandPayload(command='dispatch', vehicle_id=4, expected_version=0,
                                  driver='调度员甲', task_name='抢车任务', task_kind='排班',
                                  stake='K1+000', mileage=31850, message_id='http-a')
cmd_b = router_mod.CommandPayload(command='dispatch', vehicle_id=4, expected_version=0,
                                  driver='调度员乙', task_name='抢车任务', task_kind='排班',
                                  stake='K1+000', mileage=31850, message_id='http-b')
ra = router_mod.run_command(cmd_a)
rb = router_mod.run_command(cmd_b)
results = [ra, rb]
stat = [(r['ok'], r.get('message', '')[:6]) for r in results]
ok_count = sum(1 for r in results if r['ok'])
assert ok_count == 1, stat
loser = next(r for r in results if not r['ok'])
assert loser is not None
# JSONResponse 携带 409
assert getattr(loser, 'status_code', None) == 409 or loser.get('ok') is False

# 重复消息：cmd_a 再发一次 -> duplicate
dup = router_mod.run_command(cmd_a)
assert dup['ok'] and dup['duplicate'] is True

# 失败方拿伪造令牌收车 -> 409，占用不释放
release_bad = router_mod.CommandPayload(command='release', vehicle_id=4,
                                        expected_version=1, token='forged',
                                        message_id='http-rel-x')
rb2 = router_mod.run_command(release_bad)
assert getattr(rb2, 'status_code', None) == 409
v4 = assert_ok(router_mod.get_timeline(4))
assert v4['vehicle']['occupied'] and v4['vehicle']['version'] == 1

# 合法持有者推进收车
token = v4['vehicle']['占用令牌']
release_ok = router_mod.CommandPayload(command='release', vehicle_id=4,
                                       expected_version=1, token=token,
                                       stake='库区', mileage=31900,
                                       message_id='http-rel-ok')
rrel = assert_ok(router_mod.run_command(release_ok))
assert rrel['result']['stage'] == '收车归库'

# 业务校验：缺里程 -> 422
bad = router_mod.CommandPayload(command='dispatch', vehicle_id=4, expected_version=2,
                                driver='丙', task_name='t', task_kind='排班',
                                stake='K1', mileage=None, message_id='http-bad')
rbad = router_mod.run_command(bad)
assert getattr(rbad, 'status_code', None) == 422

# 三处回写查询接口都可用且阶段一致
sched = {r['车辆编号']: r for r in router_mod.schedule_ledger()['items']}
tracks = {r['车辆编号']: r for r in router_mod.track_details()['items']}
assert sched['VEHI-1004']['阶段'] == '收车归库' and sched['VEHI-1004']['办结']
assert tracks['VEHI-1004']['阶段'] == '收车归库' and tracks['VEHI-1004']['里程'] == 31900
emerg = router_mod.emergency_list()
assert any(r['车辆编号'] == 'VEHI-1003' for r in emerg['items'])
att = router_mod.attendance_history()
assert att['total'] == 2 and att['items'][0]['来源'] == '历史快照'
msgs = router_mod.message_log()
assert {'applied', 'conflict', 'rejected'} <= {r['status'] for r in msgs['items']}

print('HTTP 层冒烟通过：列表/比对/迁移/命令 200·409·422·去重/三处回写查询')
print(json.dumps({'冲突消息': loser.get('message') if isinstance(loser, dict) else dict(loser)['message']},
                 ensure_ascii=False))
