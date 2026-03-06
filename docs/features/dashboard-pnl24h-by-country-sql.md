# Dashboard: 近两日客户平仓净盈亏（按国家 / Sales Team）— SQL 与接口设计

## 产品与展示约定

- **卡片标题**：近两日客户平仓净盈亏；副标题「时间口径：MT Server 时间」（小号字）。
- **表格展示**：**今日Profit**、**今日IB佣金**、**昨日Profit**、**昨日IB佣金** 四列（无合计列）；默认按昨日盈亏升序；斑马纹；数值左对齐；整体字体 `text-xs`。
- **IB 佣金**：从 `stats_ib_commissions` 按客户（refId）所属 sales team 聚合，汇总所有层级 IB 的 rebate 成本；灰色展示（`text-muted-foreground`）。
- **前端**：不做 i18n，文案固定中文；国家汇总采用方案 A（前端按 `country` 分组并 SUM，PnL 和 IB 佣金各自汇总）。
- **未映射国家**：后端映射表缺失或 country 为空时，返回 `Unknown`。

---

## 1. 时间口径：按天聚合（今日 + 昨日，MT Server）

- 使用 **`stats_trading`**：按 `(date, loginSid)` 预聚合，一行 = 某账户在某日的平仓汇总。
- **净盈亏口径**：`totalPlClosed` = **PROFIT + SWAPS + COMMISSION**（已含点差/库存费与佣金），即当日平仓净盈亏。
- **时间范围**（按 **MT Server** 自然日）：
  - **昨日**：`date = CURDATE() - INTERVAL 1 DAY`
  - **今日**：`date = CURDATE()`
- 不做「过去 24 小时」滚动窗口，语义就是「今日 + 昨日」平仓数据的合计，逻辑清晰，且完全走 `stats_trading` 的 `(userId, date)` 索引，查询快。

## 2. 展示维度：仅按 Sales Team（categoryId = 6）

- **不单独处理 cid**：不再区分 cid=0(CN) / Global，统一按 **sales team** 展示。
- **数据来源**：`tags` 表中 `categoryId = 6` 的标签（sales team），通过 `user_tags` 关联到用户；一个用户取一个 tag（如 `MIN(t.tag)`），无 tag 显示为 `Unknown`。
- **国家与 Sales Team 映射**：采用 **方案 C（后端配置）**，在后端维护「sales team（tag 名）→ country」映射表；接口返回时根据该配置为每行补充 `country` 字段，前端可直接展示或按国家分组。映射表见 §6。

## 3. 推荐 SQL：今天 PnL、昨天 PnL 分列，按 Sales Team 聚合

输出三列：**今天净盈亏**、**昨天净盈亏**、**合计**（前两列之和）。口径均为 `totalPlClosed`（含 swap、commission）。

```sql
-- 今天 / 昨天平仓净盈亏分列，按 sales team（tags.categoryId=6）聚合。
-- totalPlClosed = PROFIT + SWAPS + COMMISSION。DB: fxbackoffice

SELECT
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE() THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_today,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE() - INTERVAL 1 DAY THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_yesterday,
    ROUND(SUM(by_user.pl_usd), 2) AS net_pnl_total
FROM (
    SELECT
        st.userId,
        st.date AS dt,
        SUM(IF(st.currency = 'CEN', st.totalPlClosed / 100.0, st.totalPlClosed)) AS pl_usd
    FROM stats_trading st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid AND mu.userId = st.userId
    WHERE st.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
      AND st.userId > 0
      AND st.tradeCnt > 0
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId, st.date
) AS by_user
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON by_user.userId = tt.userid
GROUP BY tt.team_tag
ORDER BY net_pnl_total DESC;
```

- **net_pnl_today**：当天（`date = CURDATE()`）的平仓净盈亏合计。
- **net_pnl_yesterday**：昨天（`date = CURDATE() - INTERVAL 1 DAY`）的平仓净盈亏合计。
- **net_pnl_total**：今天 + 昨天合计（方便排序或汇总）。
- **口径**：上述均为 `totalPlClosed`，即 **含 swap、commission**（PROFIT+SWAPS+COMMISSION）。
- **维度**：按 sales team（categoryId=6）聚合；无 tag → `Unknown`。
- **国家**：后端根据 §6 映射表，按 `sales_team`（即 tag 名）查得 `country`，写入接口响应；前端展示「国家」列或按国家汇总时使用该字段即可。

## 4. 为何用 stats_trading 就快？

- **数据量**：`stats_trading` 按天、按账户汇总，两天的行数 ≈ 有交易账户数（万级），远小于原始订单表按 24h 扫描的行数（十万～百万级）。
- **索引**：`(userId, date)` 等索引适合「按日期 + 按用户」过滤和聚合，无需扫大表。

## 4.1 IB 佣金 SQL：按客户所属 Sales Team 聚合

第二条 SQL 查询 `stats_ib_commissions`，按客户（`refId`）的 sales team 聚合所有层级 IB 的佣金成本。与 §3 的 PnL SQL 维度对齐（都以客户的 `categoryId=6` tag 分组），结果在后端按 `sales_team` key 合并。

```sql
-- IB 佣金成本 by sales team (today + yesterday). DB: fxbackoffice
-- commission = 公司支付给 IB 的 rebate (所有层级合计)
-- 按 refId (产生交易的客户) 关联 sales team, 与 PnL 维度对齐

SELECT
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN sic.date = CURDATE()
              THEN IF(sic.currency = 'CEN', sic.commission / 100.0, sic.commission)
              ELSE 0 END), 2) AS ib_commission_today,
    ROUND(SUM(CASE WHEN sic.date = CURDATE() - INTERVAL 1 DAY
              THEN IF(sic.currency = 'CEN', sic.commission / 100.0, sic.commission)
              ELSE 0 END), 2) AS ib_commission_yesterday
FROM stats_ib_commissions sic
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON sic.refId = tt.userid
WHERE sic.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
GROUP BY tt.team_tag
```

- **`stats_ib_commissions`**：PK 为 `(date, ibId, refId, currency)`，一行 = 一个 IB 从一个客户在某日某币种赚取的佣金。
- **合并逻辑**：后端在同一连接中顺序执行两条 SQL（PnL + IB Commission），按 `sales_team` key 合并到同一行。若某 team 只有 IB 佣金无 PnL，PnL 字段为 0（反之亦然）。
- **CEN 处理**：与 PnL 一致，`currency='CEN'` 时 `/100`。

## 5. 接口输出与前端使用

- **接口返回**：每行包含 **sales_team**（tag 名）、**net_pnl_today**、**net_pnl_yesterday**、**net_pnl_total**、**ib_commission_today**、**ib_commission_yesterday**，以及后端根据 §6 映射表填充的 **country**（未映射或为空时返回 `Unknown`）。
- **前端**：
  - 卡片标题「近两日客户平仓净盈亏」，副标题「时间口径：MT Server 时间」（CardDescription，小号字）。
  - 表格四列：**今日Profit**、**今日IB佣金**、**昨日Profit**、**昨日IB佣金**（无合计列）；先按 **country** 展示，默认按 **昨日** 盈亏升序排序；每行 country 可点击展开，下方展示该国家下各 **sales_team** 的同四列（方案 A：前端按 `country` 分组并 SUM；子行按昨日升序；斑马纹；数值左对齐）。
  - IB 佣金列使用灰色（`text-muted-foreground`），与 Profit 的红绿色区分。整体字体缩至 `text-xs` 以容纳更多列。

---

## 6. Sales Team → Country 映射表（后端配置，方案 C）

以下为 `tags.categoryId = 6` 的 tag 与国家的对应关系，存于后端配置（如 JSON/dict），键为 **tag 名**（与 SQL 中的 `sales_team` 一致），值为展示用国家名；未列出的 tag 或 country 为空时，后端可统一返回空串或 `Unknown`。

| tag_id | sales_team (tag 名) | country |
|--------|---------------------|---------|
| 34 | sh | CN |
| 35 | shh | CN |
| 36 | szd | CN |
| 37 | szm | CN |
| 38 | sht | CN |
| 39 | szh | CN |
| 40 | shl | CN |
| 41 | szs | CN |
| 42 | gzz | CN |
| 43 | she | CN |
| 153 | hzl | CN |
| 158 | szu | CN |
| 162 | xjm | CN |
| 169 | shy | CN |
| 170 | CN/无大场 | CN |
| 171 | jxw | CN |
| 181 | sp01 | CN |
| 189 | sht002 | CN |
| 215 | sp02 | CN |
| 11363 | HNE | CN |
| 14804 | ccx | CN |
| 18556 | EEH | — |
| 18557 | HKT | Terry |
| 36677 | SHP | CN |
| 38241 | SHT042 | CN |
| 41378 | ThaiBKK | TH |
| 43027 | THB | TH |
| 43028 | THC | TH |
| 45423 | TH_CompanyDL | TH |
| 47287 | Global_CompanyDL | — |
| 50230 | CN/Company | CN |
| 63210 | SHS | CN |
| 68341 | CS/Company | CN |
| 87419 | SHF | CN |
| 102419 | VNM | VN |
| 102752 | VNW | VN |
| 130301 | VNH | VN |
| 171612 | TWS | TW |
| 191566 | SHT049 | CN |
| 203461 | THA | TH |
| 208254 | VNJ | VN |
| 224230 | THD | TH |
| 228702 | THE | TH |
| 231261 | JSA | CN |
| 236734 | THF | TH |
| 249142 | VNS | VN |
| 250369 | TW_CompanyDL | TW |
| 260859 | VSH | CN |
| 261872 | SHT070 | CN |
| 386118 | TWK | TW |
| 410054 | TJK | TH |
| 419721 | THJ | TH |
| 488349 | JAP | Japan |
| 488376 | AFR | Africa |
| 488381 | MYS | Malay |

**说明**：
- 后端以 **sales_team（tag 名）** 为 key 查 country；若某 tag 未在表中或 country 为空（表中标为 —），统一返回 **`Unknown`**。
- 实现时建议用 `tag_name → country` 的 dict/JSON，与 SQL 返回的 `sales_team` 字段一致即可。

---

## 7. 接口命名与鉴权

- **推荐路径**：`GET /api/v1/dashboard/pnl-by-sales-team`（或由实现统一命名为 `dashboard/sales-team-pnl` 等，在路由与代码中写好注释说明用途）。
- **鉴权**：与项目其他只读展示接口保持一致，无需单独做权限控制。
- **注释**：路由与 service 层需注释说明：返回「按 Sales Team 的今日/昨日平仓净盈亏 + country」，时间口径为 MT Server 自然日，供 Dashboard「近两日客户平仓净盈亏」卡片使用。

---

## 8. Docker 环境下添加 shadcn 组件（如 collapsible）

前后端均部署在 Docker 时，**不要**在容器内执行 `npx shadcn@latest add xxx`（容器内通常没有持久化 repo，且可能缺少交互环境）。

**推荐做法**：

1. **在宿主机**（克隆了本仓库的机器）上，进入前端目录执行：
   ```bash
   cd frontend
   npx shadcn@latest add collapsible
   ```
2. 该命令会修改或新增 **宿主机** 上的文件（如 `src/components/ui/collapsible.tsx`、`components.json` 等）。这些文件属于源码的一部分。
3. **开发环境**：若使用 `docker-compose.dev.yml`，前端通过 volume 挂载 `.:/app`，宿主机上的上述变更会直接出现在容器内，无需在容器里再执行命令。
4. **生产镜像**：构建镜像时 Dockerfile 会 COPY 源码（或通过 build context 包含这些文件），新组件会随源码一起打进镜像。只需保证 `git add` 并提交这些新文件即可。

**结论**：在宿主机执行一次 `npx shadcn@latest add collapsible`，把生成的文件纳入版本控制；Docker 只负责挂载或拷贝源码，不会在容器内安装 shadcn 组件。

---

## 9. 实现说明（已落地）

- **后端**：`backend/app/api/v1/routes/dashboard.py`（GET /pnl-by-sales-team）、`backend/app/services/dashboard_pnl_service.py`（两条 SQL：PnL from `stats_trading` + IB commission from `stats_ib_commissions`，按 sales_team 合并）、`backend/app/schemas/dashboard_pnl.py`（`SalesTeamPnlRow` 含 `ib_commission_today/yesterday`）。
- **前端**：`frontend/src/components/dashboard/Past24hClientPnlByCountry.tsx`（请求接口、按国家分组、可展开查看各 sales team 明细）。
- **展示**：卡片标题「近两日客户平仓净盈亏」，副标题「时间口径：MT Server 时间」；表格列：国家/地区、今日Profit、今日IB佣金、昨日Profit、昨日IB佣金（无合计）；默认按昨日盈亏升序；点击国家行展开/收起该国家下各 sales team 的同四列；IB 佣金灰色展示；整体字体 `text-xs`；斑马纹；数值左对齐。

### 9.1 数据过滤（与客户类报表统一）

- **排除 demo 账户**：`mt4_users.GROUP NOT LIKE '%demo%'`，且 `sid IN (1, 5, 6)`。
- **排除 employee 账户**：`INNER JOIN users u ON u.id = st.userId AND COALESCE(u.isEmployee, 0) = 0`（仅保留非员工）。后续新增客户/交易类报表时，应同样排除 demo 与 employee，参见 database-context skill 的 Business Conventions。
