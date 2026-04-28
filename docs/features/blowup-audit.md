# Blowup Audit — 爆仓客户 / AB 仓离线审计

> 离线、按需的"爆仓客户 + AB 对家配对"审计工具，**不在生产调度里**，给风控/合规临时跑某个 MT 时间窗口用。
>
> 入口脚本：`backend/scripts/blowup_audit_window.py`
> AI / Cursor 使用指南：`.cursor/skills/blowup-audit/SKILL.md`

## 1. 做什么

针对任意 **MT 服务器时间** 窗口，输出两类风险信号：

1. **爆仓客户清单** —— 窗口内有亏损平仓 (`totalProfit < 0`) 且账户当前 `mt4_users.BALANCE < 0` 的账户  
   伴随小时分布、累计亏损、最差余额；带噪音过滤阈值。
2. **AB 对家配对（gap-trading 嫌疑）** —— 给每笔亏损单找：同品种、反向、`OPEN_TIME` 相近、对家盈利的对家  
   默认要求**对家与亏方同 CRM clientid**（同人多账号自对冲），可切换到「跨 clientid」。

业务背景 / 强平不算 AB 的判断，详见 [`login-ip.md` §3.3](./login-ip.md#33-ab-仓与系统强制平仓--强平要不要算在一起)。

## 2. 数据来源

- `fxbackoffice.mt4_trades` — 交易明细
- `fxbackoffice.mt4_users` — 余额、客户、组别快照

> **不读**任何 MT 服务器日志，`mt4_trades` 不存逐笔 IP，所以本脚本**不带 IP 维度**。如果要做"同 IP 配对"，得回到日志侧（见 `login-ip.md`）。

## 3. 时区

- `--start-mt` / `--end-mt` **全部是 MT 服务器时间** (UTC+3)
- `mt4_trades.OPEN_TIME` / `CLOSE_TIME` 也是 MT 时区 `DATETIME`，无 tz 信息，直接比较
- HKT = MT + 5h；MT `2026-04-27 00:00 ~ 02:00` 对应 HKT `05:00 ~ 07:00`
- 启动时第一行 `[INIT] window(MT): ...` 日志固定写 "MT"，肉眼复核

## 4. 关键 flag

| flag | 默认 | 作用 |
|------|------|------|
| `--start-mt "YYYY-MM-DD HH:MM[:SS]"` | 无 | MT 时区窗口起点 |
| `--end-mt   "YYYY-MM-DD HH:MM[:SS]"` | 当前 MT | MT 时区窗口止点 |
| `--hours-back N` | `24` | `--start-mt` 没传时往前推 N 小时 |
| `--sid` | `5` | 服务器列表，逗号分隔。`1`=MT4 / `5`=MT5 / `6`=CEN，例：`--sid 1,5,6` |
| `--min-acc-loss-usd` | `50.0` | 窗口内累计亏损 (USD-eq) 绝对值 < 阈值的账户丢掉，`0` 关闭 |
| `--exclude-demo-test` / `--no-...` | `true` | `groupsid` / `NAME` 含 `demo` / `test` 的账户排除（亏方与对家两端都过滤） |
| `--same-client-only` / `--no-...` | `true` | AB 对家必须与亏方同 CRM clientid（推荐保持 true） |
| `--max-open-diff-sec` | `60` | AB 对家 `OPEN_TIME` 容差（秒） |
| `--min-lot-ratio` / `--max-lot-ratio` | `0.5` / `2.0` | AB 对家 / 亏方手数比上下限 |
| `--send-email` / `--no-...` | `false` | 发送 HTML 邮件 + Excel 附件 |
| `--mail-to` | `.env` `BLOWUP_AUDIT_MAIL_TO` | 多收件人逗号分隔 |
| `--mail-cc` | `.env` `BLOWUP_AUDIT_MAIL_CC` | 抄送 |
| `--out PATH` | 自动 | 默认 `backend/scripts/blowup_audit_<END_MT>.xlsx` |

CEN 账户 (`SYMBOL` 后缀 `.cent` / `.kcmc`) 的 `PROFIT` / `BALANCE` 自动按 cent → USD-eq 归一（÷100）。

## 5. 输出 Excel（5 sheet）

| Sheet | 内容 |
|-------|------|
| `summary` | 本次跑的参数 + 各阶段行数 + 总亏损 USD |
| `hourly` | 每小时：亏损单数、distinct 爆仓账户、distinct 客户、亏损合计、最差单笔、最负余额 |
| `blown_accounts` | 爆仓账户去重清单（`loginSid`、`userid`、`name`、`groupsid`、累计亏损） |
| `blown_trades` | 这批账户在窗口内的**全部**平仓单（含赢的，便于看是否有对冲） |
| `ab_pairs` | 对家配对（亏方 ↔ 对家），含 `open_diff_sec` / `lot_ratio` / `net_usd` |

## 6. 邮件配置

收件人放 `backend/.env`，命令行 `--mail-to` 可覆盖：

```env
BLOWUP_AUDIT_MAIL_TO=kieran.xiang@kohleservices.com
BLOWUP_AUDIT_MAIL_CC=
```

SMTP 与平台其它邮件复用同一组凭据：`SMTP_SERVER` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD`。底层走 `app.services.email_service.send_email`（详见 `email-notification` skill）。

模板用了内联 `<table>` 样式，**Outlook 兼容**；不要切回 `pandas.to_html(...)` 默认输出（Outlook 会丢大量 CSS）。

## 7. 步骤日志

每次跑都会打印 `[HH:MM:SS] [STEP{N}]`，定位最慢的环节通常是 `STEP4`（AB 对家自连接）：

```
[INIT]  window(MT): 2026-04-27 00:00:00 -> 2026-04-27 02:00:00
[STEP1] Hourly raw rows: 65
[STEP2] Blown account rows: 5
[STEP3] Blown trade rows: 59
[STEP4] AB pair rows: 100        ← self-join，最慢
[STEP5] Excel written: ...
[STEP6] Email sent / skipped
[DONE]  Total elapsed: ...
```

## 8. 常用命令

激活项目 venv（**不要** `pip install --user`，遵循工作区规则）：

```bash
cd /opt/myproject/New-IT-System/backend
source .venv/bin/activate
```

最近 24h，仅 MT5：

```bash
python scripts/blowup_audit_window.py --hours-back 24 --sid 5
```

固定窗口、3 个服务器（MT4 + MT5 + CEN）、发邮件：

```bash
python scripts/blowup_audit_window.py \
  --start-mt "2026-04-27 00:00:00" \
  --end-mt   "2026-04-27 02:00:00" \
  --sid 1,5,6 \
  --send-email
```

只生成 Excel 不发邮件（默认行为）：

```bash
python scripts/blowup_audit_window.py \
  --start-mt "2026-04-27 00:00:00" \
  --end-mt   "2026-04-27 02:00:00" \
  --sid 5
```

## 9. 排错

| 现象 | 排查 / 处理 |
|------|------------|
| `TypeError: not enough arguments for format string` | LIKE 字面量里 `%` 没 escape；pymysql 用 `%`-formatting，必须写 `'%%text%%'`。 |
| `mail-to is empty` | 没传 `--mail-to`，且 `.env` `BLOWUP_AUDIT_MAIL_TO` 为空。 |
| Outlook 邮件样式错乱 | 不要改回 `df.to_html(...)`；Outlook 会吃掉大量 CSS。维持内联 table 写法。 |
| `STEP4` 很慢（>60s） | 缩窗口 / 提高 `--min-acc-loss-usd` / 减小 `--max-open-diff-sec`。命中索引 `INDEX_CLOSEDATE` + `IDX_OPEN_DATE`。 |
| 0 个爆仓账户 | 多半时区传错（HKT 当成 MT），看 `[INIT] window(MT): ...` 复核。 |
| 历史已被清零的爆仓案例漏 | `BALANCE` 是当前快照；曾经 `< 0` 后被运营清零的账户检不到，已知局限。 |

## 10. 产物不进 git

`backend/scripts/blowup_audit_*.xlsx` 含客户 PII，已在 `.gitignore` 全局排除 `*.xlsx`。跑完发邮件 / 归档后可以本地删；脚本随时能重跑。

## 11. 关联文档

- [`login-ip.md`](./login-ip.md) §3 数据源 / §3.3 AB 仓口径
- [`risk-monitor.md`](./risk-monitor.md) — 实时风控（burst-open / batch-open），与本脚本互补：实时监控 vs. 离线复盘
- `.cursor/skills/blowup-audit/SKILL.md` — Cursor / AI agent 使用指南
- `.cursor/skills/email-notification/SKILL.md` — 邮件基础设施
