# Dashboard · 客户平仓净盈亏历史

「近两日客户平仓净盈亏」卡片的扩展视图,支持查看最近最多 30 天的趋势与按国家/Sales Team 的明细。

## 入口

- Home dashboard → 「近两日客户平仓净盈亏」卡片右上角工具栏 → 「历史」按钮(刷新按钮左侧)。
- 路由:`/dashboard/pnl-history`,可直接收藏/分享。

## 视图

页面三块,自上而下:

1. **工具栏**:快捷范围 7d / 14d / 30d、日期范围 Popover(`Calendar mode="range"`)、复选框「扣除 IB 佣金」、查询耗时与行数。
2. **柱状图**:横轴为日期,纵轴为每日 Profit,按国家堆叠分色。Toggle ON 时每个国家段高度 = Profit − IB,看公司真实净收入。
3. **明细表格**:每行 = (日期, 国家),展开后看该日该国下各 Sales Team 的 Profit / IB / 净额。

URL 参数同步:`?from=YYYY-MM-DD&to=YYYY-MM-DD&deduct_ib=1`。

## 数据口径

与 dashboard 卡片完全一致(`docs/features/dashboard-pnl24h-by-country-sql.md`):

- **Profit (excl. rbt)**:`fxbackoffice.stats_trading.totalPlClosed`(已含 swap + commission,**不**扣 IB 返佣)。CEN 货币除以 100 转 USD。
- **IB 佣金**:`fxbackoffice.stats_ib_commissions.commission`,按客户(`refId`)的 sales team(`tags.categoryId=6`)聚合所有层级 IB 返佣。
- **国家**:由后端 `SALES_TEAM_TO_COUNTRY` 映射(单一来源:`dashboard_pnl_service.SALES_TEAM_TO_COUNTRY`)。
- **过滤**:`sid IN (1,5,6)`、排除 `%demo%` GROUP、排除 `users.isEmployee=1`、`tradeCnt > 0`。
- **时间**:`stats_trading.date`(MT Server 自然日)。

## 性能与限制

- 后端无 Redis 缓存,**每次点击直查 MySQL 主库**。
- 单次 30 天查询实测 ~1.3s(~600 行),7 天 ~0.5s(~130 行)。`stats_trading` 走 `IDX_ACCDATE(loginSid, date)` 范围扫描。
- **硬上限 30 天**:Pydantic `model_validator` 强制 `(date_to - date_from) <= 29 天`,前端 Calendar `disabled` 回调同步禁止 30 天外选取,双保险。
- 前端 `AbortController` 处理 StrictMode 双重渲染,日期切换时取消上一次请求。

## API

`GET /api/v1/dashboard/pnl-history?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`

返回:
```json
{
  "rows": [
    {
      "date": "2026-05-05",
      "sales_team": "AFR",
      "country": "Africa",
      "profit_excl_rbt": -252.82,
      "ib_commission": 18.78
    }
  ],
  "date_from": "2026-05-05",
  "date_to": "2026-05-11",
  "statistics": { "query_time_ms": 455 }
}
```

错误:
- `422` 范围超 30 天 / 终点早于起点 / 终点为未来日期。
- `500` 数据库异常(详情写入 backend 日志)。

## 关键文件

| 文件 | 作用 |
|---|---|
| `backend/app/schemas/dashboard_pnl_history.py` | `PnlHistoryQuery`(30 天校验)、`PnlHistoryRow`、`PnlHistoryResponse` |
| `backend/app/services/dashboard_pnl_history_service.py` | 两条 SQL(stats_trading + stats_ib_commissions)+ Python 层 merge + 国家映射 |
| `backend/app/api/v1/routes/dashboard.py` | `/pnl-history` 路由 + Pydantic 422 处理 |
| `frontend/src/pages/DashboardPnlHistory.tsx` | 工具栏 / Recharts 堆叠柱状图 / 折叠表格 |
| `frontend/src/components/dashboard/Past24hClientPnlByCountry.tsx` | 卡片头部「历史」按钮 |
| `frontend/src/App.tsx` | 路由 `/dashboard/pnl-history` + `lazyWithRetry` |

## 移动端适配

- Calendar:`numberOfMonths` 自适应(<640 显示 1 月,否则 2 月)。
- 柱状图:`overflow-x-auto` + `minWidth = max(360, days × 44px)`,横向滑动看完整 30 天。
- 表格:`overflow-x-auto`;「净额」列在 `<sm` 隐藏;日期在 `<sm` 显示 `MM-DD` 简写。
- 工具栏:`flex-col` → `sm:flex-row` 自动堆叠。

## 后续可扩展点(非本次范围)

- 按 GROUP 展开(对应现有 `pnl-by-group` 接口的历史版本)。
- 数据导出 CSV。
- 增加「同比/环比」对比柱状图。
