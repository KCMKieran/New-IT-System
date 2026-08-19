---
id: OPT-0059
title: fund_flow_monitor 8 个路由全是 async def 但内部是阻塞 SQLite/MySQL 调用
status: wip
priority: P2
area: backend
effort: S
created: 2026-08-19
related: [[OPT-0055]]
---

## 问题

`backend/app/api/v1/routes/fund_flow_monitor.py` 里**全部 8 个路由都声明成 `async def`**，
但它们调用的每一层都是**同步阻塞 IO**：

| 行 | 路由 | 阻塞在哪 |
|---|---|---|
| 128 | `GET /snapshot/latest` | `get_latest_snapshot()` → `sqlite3.connect` (`core/fund_flow_monitor_db.py:114,141`) |
| 139 | `GET /scans` | 同上 |
| 145 | `POST /scan-now` | `trigger_scan_now()` → **pymysql 全量扫描**（`services/fund_flow_monitor_service.py:44`），秒级～分钟级 |
| 182 | `POST /query` | ad-hoc 查询，pymysql + ThreadPoolExecutor |
| 316 | `GET /detail/{user_id}` | pymysql |
| 342 | `GET /config` | sqlite |
| 372 | `POST /config` | sqlite 写 |
| 426 | `GET /export` | sqlite 读 + CSV 生成 |

`async def` 里直接调同步 DB = **在事件循环线程上阻塞**，整个 uvicorn worker 期间不能处理
任何其它请求。CLAUDE.md 的约定写得很直白：「阻塞 IO 的路由必须 `def` 不要 `async def`」，
OPT-0055 实测过后果：一个 1.3s 的接口被并发拖到 2.7–5.2s。

`scan-now` 是这里最危险的一个——它跑的是全量 MySQL 扫描（秒级到分钟级），
一次点击就能让该 worker 上**所有**其它请求排队。

## 怎么发现的

2026-08-19 修 fund-flow 导出 403（`fix/fund-flow-csv-403`，merge `722063b`）时顺带看到。
当时只改了被要求修的那一处（下载路径 + CSV BOM），**没动 async**：
只改 8 个里的 1 个会让文件内部不一致，改全部 8 个又超出「修 403」的范围、
且其中 7 个当时没有测试覆盖也没被验证过。故 file 成独立 OPT。

## 修法

把 8 个 `async def` 改成 `def`。FastAPI 会自动把同步 handler 丢进 threadpool，
事件循环不再被占住。**没有别的改动**——不要顺手改成真 async（那要换掉 pymysql/sqlite3，
是另一个数量级的工作，且本项目 service 层整体是同步的）。

⚠ 注意 `Depends(get_auditor)` / `Query(...)` 在同步 handler 下行为不变，不需要调整。

## AC

- [ ] `fund_flow_monitor.py` 内 8 个路由全部为 `def`（`grep -c "^async def" == 0`）
- [ ] 8 个端点手工过一遍：snapshot/latest · scans · scan-now · query · detail · config(GET/POST) · export
      —— 该模块**当前零后端测试**（`ls backend/tests | grep fund` 无结果），所以手测是唯一验证手段
- [ ] `./verify.sh` 绿
- [ ] 顺带确认：全仓是否还有同型违规（`grep -rn "^async def" backend/app/api/v1/routes/` 逐个核对
      是否真的 await 了异步库）——若还有别的文件命中，在本 OPT 的结果段列出来，
      **不要**在本 OPT 里一起改（各自 file）

## 开放问题

1. **要不要给这个模块补后端测试**？现在 0 个测试，改完只能靠手测。补一份 smoke 测试
   （8 个端点各打一次、断言非 5xx）大概 S，但会把 effort 抬到 M。待用户拍板。
2. 全仓同型扫描如果命中很多（比如十几个文件），是否值得升级成一个「全仓 async/def 审计」
   的大 OPT，而不是逐文件 file。
