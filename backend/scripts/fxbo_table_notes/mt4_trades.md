- CMD: 0=Buy 1=Sell 2/3=Limit 4/5=Stop 6=Balance（出入金调整，不是交易）
- VOLUME÷100=手数（或直接用生成列 lots）；totalProfit 生成列=PROFIT+SWAPS+COMMISSION（仅 CMD 0/1/6）
- sid: 1=mt4_live 5=mt5 6=mt4_live2；loginSid={SID}-{LOGIN}（索引）、ticketSid={SID}-{TICKET}（PK）
- 未平仓：CLOSE_TIME='1970-01-01'（closeDate 同）
- ⚠ OPEN_TIME/CLOSE_TIME 是 MT 时区 UTC+3 无 DST，且 OPEN_TIME 无直接索引——范围查询走 openDate/closeDate（STORED+索引），时分粒度内存再过滤
- ⚠ 性能：日期条件不要 OR（破索引）改 UNION ALL；避免自连接（用临时表/应用层窗口检测）
- CEN 组金额是 cents ÷100（币种看 mt4_users.CURRENCY）

## ⚠ sid=5 (MT5) 是镜像表，两个坑

- **TICKET 是独立编号，≠ MT5 PositionID**。同一笔单：MT5 PositionID `33801750` / 本表 TICKET `30406228`。
  所以拿 MT5 侧的单号（日志 `#N`、报表 Position ID）来查本表 **一条都查不到**——不是数据缺失，是 join 错表。
  对 MT5 单请直接查 `mt5_live`，别绕本表：
  ```sql
  -- 平仓 deal：走索引 IDX_POSITION (Login, PositionID)，Entry 1=Out 2=InOut 3=OutBy
  SELECT Deal, Login, PositionID, Entry, Action, Symbol, Time
  FROM mt5_live.mt5_deals
  WHERE (Login, PositionID) IN ((60006040, 33800410), ...) AND Entry IN (1,2,3);
  -- 开仓单 / PositionID 溯源：mt5_live.mt5_orders_history.`Order`（开仓单 Order ticket == PositionID）
  ```
  2026-07-15 用 9 天 login-ip close 日志 × 10,400 单交叉验证确认（MT5 4,360 单走 mt5_deals 命中 99.86%）。
- **已平仓行的 CMD 是出场方向（与持仓相反）**：buy 仓平掉后 CMD=1、sell 仓平掉后 CMD=0；**未平仓行仍是入场方向**。
  MT4(sid 1/6) 开/平都是持仓方向，不受影响。2026-07 用客户 136805 的 261 笔 MT5 单 100% 系统性反转
  + 12,250 个 XAUUSD 快照交叉验证（翻转口径中位误差 0.008 手 vs 不翻转 5.21 手）。
  → 任何基于**已平仓** MT5 数据算多空方向的报表/回溯，sid=5 的行方向要取反。线上快照/持仓页只查未平仓，不受影响。
