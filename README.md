# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆档案 | 车辆编号、车辆类型、车牌号 |
| 调度时序 | `dispatch` | 车辆调度时序图 | 车辆编号、连续节点（驾驶员/任务/桩号/里程）、版本号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 车辆调度时序图（重构说明）

旧实现里同一辆养护车在「排班表、轨迹回放、应急汇总」三处各写各的阶段，
调度员无法判断车辆是否已被占用。重构后三页全部变成**调度时序**的只读投影：

- 每辆车只有一条时序：驾驶员 → 任务 → 里程（出车）→ 桩号（到场）→ 里程（收车）。
  阶段只允许沿 `待命 → 已派车 → 已出车 → 到场作业 →（收车）待命` 推进。
- **排班台账** `GET /api/dispatch/schedule-ledger`、**应急清单**
  `GET /api/dispatch/emergency`、**轨迹详情** `GET /api/dispatch/tracks`
  均由时序回写，三个页面的阶段不可能再分叉；养护车辆台账页同样回写调度阶段。
- 历史数据通过转换流程收敛：
  - `GET /api/dispatch/migration/preview` 转换前**逐车比对**三页阶段并报告冲突；
  - `POST /api/dispatch/migration/run` 执行转换（幂等）：历史出勤按**原驾驶员与
    车辆快照**冻结到 `migration/history`，**未结任务**迁入时序并从最靠后的已知
    阶段重建连续节点；冲突按"最靠后作业阶段"收敛。
  - 迁移进来的未结任务挂名「历史迁移」，现职调度员需先 `POST /api/dispatch/takeover`
    接管，才能继续推进或收车。
- 并发与消息口径（见 `backend/test_dispatch.py` 的 53 项断言）：
  - 占用车辆与挂驾驶员/任务首节点在同一把锁内**原子成功**；
  - 每条写消息必须带 `message_id`，**先去重再应用**，重复消息返回 409、不重复落地；
  - 每辆车维护单调递增的 `version`，占用/推进/释放/接管都要携带读到的版本号做
    **CAS**：并发调度只有先取得该版本号的一方成功，版本过期返回 409；
  - 释放（收车/撤销）必须由**占用者本人**持正确版本号执行；校验失败时车辆阶段、
    占用者、版本号全部保持不变，不会误释放他人占用。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 调度类写入（`dispatch`）冲突返回 409，前端提示刷新版本号后重试；领域逻辑可离线验证：
  `cd backend && python3 test_dispatch.py`（仅依赖标准库）。
