---
id: OPT-0056
title: 全站路由 detail=str(exc) 异常原文泄漏清理 —— 内部细节留日志、对外只回通用文案
status: wip
priority: P1
area: backend
effort: S
created: 2026-07-24
related: [[OPT-0047]]
---

## 背景

多个 API 路由在异常处理里把**异常原始文本**直接塞进返回给客户端的 HTTP 响应：

```python
except Exception as exc:
    raise HTTPException(status_code=500, detail=str(exc))   # ← 泄漏
```

`str(exc)` 会把底层库（psycopg2 / PyMySQL / ClickHouse driver）的原话喷给浏览器，
典型泄漏内容：

- **DB 主机名 / IP / 端口 / 账号名**：
  `connection to server at "kcm-prod.postgres.database.azure.com" (10.x.x.x), port 5432 failed: FATAL: password authentication failed for user "risk_ro"`
- **库表结构 / 列名 / SQL 片段**：
  `column "daily_user_rebate.rebate_all" does not exist\nLINE 3: SELECT r.user_id, SUM(...)`
- 文件路径、栈帧、依赖版本等指纹。

对一个**风控系统**尤其敏感：接口虽有共享 X-API-Key 门锁，但 key 一旦泄露或内部越权，
错误信息就成了摸清后端拓扑（DB 地址、schema、账号）的免费情报。而这些细节对正常用户
毫无用处——用户只需要「出错了，稍后重试」。

本 OPT 来源：risk-watchlist 独立冷审（2026-07-24），已顺手修掉 risk-watchlist 用到的
`risk_cases.py`（4 处，commit `621406e`），并 grep 出全站还有一大批同类。

## 参考实现（已落地，照抄这个模式）

`backend/app/api/v1/routes/risk_cases.py` 已改成标准做法（可作 SSOT）：

```python
except Exception as exc:
    logger.exception("watchlist query failed")   # 完整异常+栈进服务器日志(排障用)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="internal error while querying watchlist",   # 对外通用、无信息量
    )
```

要点：
1. 排障需要的**全细节走 `logger.exception(...)`**（进内网日志，你能控制的地方），
   带上有用的上下文（如 `logger.exception("case detail query failed for user_id=%s", user_id)`）。
2. 对外 `detail` 换成**泛化文案**，语气沿用各文件既有 503 分支的措辞（本项目多为
   terse lowercase 英文，如 `"internal error while querying <thing>"`）。
3. **只改 500（服务器内部错误）**。4xx（400/404/409/422）的 `detail` 往往是**有意的
   校验/业务提示**（如「参数非法」「资源不存在」），那是给用户看的正常信息，**不要动**
   ——除非该 4xx 也在拼 `str(exc)` 暴露底层原文（见下方清单标注）。
4. 每个文件确认已有 `logger = logging.getLogger(__name__)`，没有就按 repo 约定加。

## 涉及文件清单（grep 实测 2026-07-24，共 73 处 `detail=str(exc)`/`str(e)`）

**500 类（本 OPT 主体，必改）** —— 按占比排序：

| 文件 | 500 处数 | 备注 |
|---|---|---|
| `routes/risk_monitor.py` | ~42（内含 40 个 HTTP_500） | 最大户，逐个过；注意里面也有 1 个 404 / 1 个 409 是有意校验，别误伤 |
| `routes/alert_mail.py` | 9（404/409/422/502 混合） | **多数是 4xx 有意提示**，只挑真正 `str(exc)` 暴露底层的 500/502 改 |
| `routes/ib_data.py` | 4 | 含 34/68 行的 400（校验，保留），37/71 的 500 要改 |
| `routes/dashboard.py` | 4（45/59/88/121 均 500） | 全改 |
| `routes/fund_flow_monitor.py` | 3（152/272/315 均 500） | 全改 |
| `routes/pnl_summary.py` | 2（57/184 均 500） | 全改 |
| `routes/ib_financial.py` | 2 | 84 行 500 改；172 行 400 校验保留 |
| `routes/zipcode.py` | 1（69 行 500） | 改 |
| `routes/xauusd_positions.py` | 1（63 行 400） | 若是暴露底层原文则改，纯校验则保留——看现场 |
| `routes/trading_analysis.py` | 1（42 行 500） | 改 |
| `routes/hourly_details.py` | 1（39 行 500） | 改 |
| `routes/etl.py` | 1（150 行 500） | 改 |
| `routes/client_return_rate.py` | 1（86 行 400） | 看现场：暴露原文则改 |
| `routes/client_pnl.py` | 1（47 行 422） | 看现场：暴露原文则改 |

> 逐文件重新 grep 定位（行号会随改动漂移）：
> ```bash
> cd backend && grep -rn "detail=str(exc)\|detail=str(e)\|detail=f\"{exc}\|detail=f\"{e}" app/api/v1/routes/*.py
> ```
> 每处判断包着它的 `status_code`：500/502 → 改；4xx 且 detail 是**人写的校验提示** → 保留；
> 4xx 但 detail 在拼 `str(exc)` 暴露底层 → 也改（换成人写的提示）。

**已修（勿重复）**：`routes/risk_cases.py`（4 处，OPT-0047 冷审顺手，commit `621406e`）。

## 验收标准（AC）

1. 全站 `app/api/v1/routes/*.py` 中，**所有 500/502 响应**不再包含 `str(exc)`/`str(e)`/
   `f"{exc}"` 形式的异常原文；改为 `logger.exception(...)` + 泛化 `detail`。
2. 4xx 的**有意校验提示**保持不变；仅当 4xx 也在暴露底层异常原文时才一并改。
3. `grep -rn "detail=str(exc)\|detail=str(e)" app/api/v1/routes/*.py` 对 500 分支归零
   （剩余命中都应是明确判定「这是给用户的校验信息」的 4xx，在 PR 描述里逐条说明为何保留）。
4. `verify.sh` 绿（或按项目惯例：`pytest` 后端相关测试 + `tsc`/`vitest` 前端不受影响）。
   本改动是纯服务端行为，前端无关。
5. 抽查 2-3 个改过的路由：制造一个异常（如断开 DB），确认响应体是通用文案、
   服务器日志里有完整栈。

## 用户已拍板决策（2026-07-24，claim 时确认）

1. **`alert_mail.py` L125 的 502（`MailSendFailed`）要改**：它很可能裹了 SMTP 服务器原始报错
   （主机/端口/认证细节）→ 换成泛化文案（如 `"mail delivery failed"`）。alert_mail 其余
   404/409/422（catch `svc.InvalidSubscription`/`SubscriptionNotFound`/`NoRenderableAlert`
   自定义业务异常，人写提示）**全部保留**。
2. **不抽 helper，就地写字符串**：各文件就地写泛化 `detail`，不引入 `internal_error_detail()`
   抽象（改动更局部）。

### 逐文件现场核对结论（2026-07-24 grep + 逐处看 status_code）

**必改（500/502，≈57 处）**：
- `risk_monitor.py`：40 处 HTTP_500 全改；**保留** L1906(404) / L2464(409)（有意校验）
- `dashboard.py` 4（全 500）、`fund_flow_monitor.py` 3（全 500）、`pnl_summary.py` 2（全 500）
- `ib_data.py`：L37/L71 的 500（RuntimeError）改；**保留** L34/L68 的 400（ValueError 校验）
- `ib_financial.py`：L84 的 500 改；**保留** L172 的 400（ValueError 校验）
- `alert_mail.py`：**仅 L125 的 502** 改（见决策 1）
- `zipcode.py` L69 / `trading_analysis.py` L42 / `hourly_details.py` L39 / `etl.py` L150：各 1 处 500，改

**确认保留（有意 4xx 校验，勿动）**：
- `xauusd_positions.py` L63(400)：`validate_export_range` 的 ValueError（范围校验）
- `client_return_rate.py` L86(400)：代码已有注释 `# ...(Claude-authored) validation message; safe to expose`
- `client_pnl.py` L47(422)：filter 校验（`join must be 'AND' or 'OR'` 等人写提示）
- `ib_data.py` L34/L68、`ib_financial.py` L172、`alert_mail.py` 404/409/422、`risk_monitor.py` L1906/L2464

> ⚠ 行号会随改动漂移，实施时对每个文件重新 grep 定位。

## 开放问题

- **文案统一度**：要不要抽一个 `internal_error_detail(thing: str)` 小 helper 统一措辞，
  还是各文件就地写字符串？倾向就地写（改动更局部、无新抽象），但 73 处规模下 helper
  也合理——实施者按手感定，不阻塞。
- **是否顺带加全局 exception handler**：FastAPI 可注册一个兜底 `@app.exception_handler`
  把未捕获异常统一转成通用 500 + 日志，从根上防漏。这是**更大范围的架构改动**（会影响
  所有未来路由），**不在本 OPT 范围**——若实施者认为值得，单独 file 一个 follow-up OPT，
  别在本 OPT 里顺手加（避免范围蔓延）。

## 结果

（完成时填：实际改了哪些文件/处数、保留的 4xx 清单及理由、follow-up）
