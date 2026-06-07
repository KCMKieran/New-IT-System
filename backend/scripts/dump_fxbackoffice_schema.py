"""
Dump fxbackoffice schema into the tiered context library used by the
`database-context` skill (OPT-0036).

Two modes (because the skill dir `.cursor/` is not mounted into the backend
container, while pymysql is only available inside it):

  fetch  — run INSIDE the backend container; reads information_schema with the
           readonly account and prints a JSON snapshot to stdout.

      docker exec new-it-backend-dev \
          python /app/scripts/dump_fxbackoffice_schema.py fetch > /tmp/fxbo_schema.json

  render — run ON THE HOST (stdlib only); turns the JSON snapshot into
           markdown: one `_index.md` (3 tiers) + one file per Tier-A table.
           Content between `<!-- manual -->` ... `<!-- /manual -->` in existing
           output files is preserved verbatim across re-renders.

      python3 backend/scripts/dump_fxbackoffice_schema.py render \
          --json /tmp/fxbo_schema.json \
          --out .cursor/skills/database-context/fxbackoffice

Tier rules:
  A — whitelist below (tables actually referenced by backend SQL, or already
      curated in the old mysql-schemas.md). Full per-table detail file.
  B — not Tier A, (data+index) size > 10 MB, not noise. One index line each:
      the point is "this table is big — don't full-scan it".
  C — everything else, grouped by name prefix, one line per group.
      tmp_* / *_old / *_backup / 0-row tables are bucketed as ignorable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = "fxbackoffice"

# Tier A: gets a per-table detail file under tables/.
# Sources: precise grep over backend/app (FROM/JOIN/UPDATE context) + tables
# already hand-curated in the pre-OPT-0036 mysql-schemas.md.
TIER_A = [
    "mt4_trades",
    "mt4_users",
    "users",
    "groups",
    "transactions",
    "transfers",
    "stats_transactions",
    "stats_trading",
    "stats_trading_running_totals",
    "stats_trading_running_totals_user",
    "stats_balances",
    "stats_ib_commissions",
    "stats_ib_commissions_by_login_sid",
    "stats_ib_commissions_by_rule_type",
    "stats_custom_kohle_ref_lots",
    "stats_ib_data",
    "tags",
    "user_tags",
    "user_custom_fields",
    "psps",
    "ib_tree",
    "ib_tree_with_self",
]

# One-line purposes for the index (Tier A required; Tier B best-effort).
PURPOSE = {
    "mt4_trades": "MT4 全量订单（开/平仓），风控与审计的核心事实表",
    "mt4_users": "MT4 账户主表（GROUP/CURRENCY/AGENT_ACCOUNT）",
    "users": "CRM 客户主表；isEmployee 员工过滤源",
    "groups": "MT 组配置；demo 组过滤源（GROUP NOT LIKE '%demo%')",
    "transactions": "出入金支付流水（PSP 级，每行一笔）",
    "transfers": "账户间内部转账",
    "stats_transactions": "出入金日聚合统计",
    "stats_trading": "每日每账户交易聚合（date, loginSid）",
    "stats_trading_running_totals": "每账户累计交易盈亏（原币种）",
    "stats_trading_running_totals_user": "每客户累计交易盈亏（EUR 计价！）",
    "stats_balances": "每日账户 balance/equity/credit 快照",
    "stats_ib_commissions": "IB 日佣金（date, ibId, refId, currency）",
    "stats_ib_commissions_by_login_sid": "IB 佣金按 MT 账户维度",
    "stats_ib_commissions_by_rule_type": "IB 佣金按规则类型拆分",
    "stats_custom_kohle_ref_lots": "IB 下线日交易手数（全部 vs 直推）",
    "stats_ib_data": "IB 活跃客户数快照",
    "tags": "标签定义（含风控 tag）",
    "user_tags": "客户-标签多对多关联",
    "user_custom_fields": "客户自定义字段（KV）",
    "psps": "PSP 支付通道配置；cid=0 即 CN 通道",
    "ib_tree": "IB 层级闭包表（ancestor/descendant）",
    "ib_tree_with_self": "IB 层级闭包表含自身行",
    # Tier B best-effort
    "ib_processed_tickets": "IB 佣金计算过的 ticket 明细（计算引擎内部）",
    "stats_daily_snapshot": "每日账户全量快照（计算引擎内部）",
    "ib_iteration_rules": "IB 佣金迭代×规则展开明细",
    "ib_iteration_tickets": "IB 佣金迭代×ticket 关联",
    "ib_iterations": "IB 佣金计算批次",
    "ib_rule_settings": "IB 规则参数设置",
    "ib_rules": "IB 佣金规则定义",
    "ib_rules_conditions": "IB 规则触发条件",
    "user_log_emails": "发给客户的邮件日志（含正文，大表）",
    "manager_log": "CRM 后台操作日志",
    "transactions_log": "transactions 变更日志",
    "transactions_log_old": "transactions 旧版变更日志（历史遗留）",
    "transactions_meta": "transactions 附加元数据",
    "documents": "客户上传文档（KYC 等）",
    "plugin_loyalty_transactions": "积分/忠诚度流水",
    "ecb_prices": "ECB 汇率历史",
    "mass_mails_users": "群发邮件×用户投递记录",
    "applications": "开户申请",
    "wallets": "客户钱包账户",
    "user_login_log": "客户门户登录日志",
    "kyc_requests": "KYC 审核请求",
    "mt4_users_snapshot": "mt4_users 快照",
    "mt4_users_log": "mt4_users 变更日志",
    "stats_trading_lots": "每日手数聚合",
    "stats_trading_lots_by_instrument": "每日手数按品种聚合",
    "stats_transactions_by_psp": "出入金按 PSP 日聚合",
    "related_profiles": "关联客户档案（同 IP/设备等）",
}

TIER_B_MIN_BYTES = 10 * 1024 * 1024  # 10 MB
NOISE_RE = re.compile(r"(^tmp_|_old$|_bak$|_backup$|_copy$)", re.I)
MANUAL_RE = re.compile(r"<!-- manual -->\n(.*?)<!-- /manual -->", re.S)
MANUAL_PLACEHOLDER = "（业务注释：暂无 — 沉淀后写在这区，重跑生成器不会丢）\n"


# --------------------------------------------------------------------------
# fetch mode — runs inside the backend container
# --------------------------------------------------------------------------

def fetch() -> dict:
    import os

    import pymysql  # only importable inside the container

    conn = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        port=int(os.environ.get("MYSQL_PORT", 3306)),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=120,
    )
    out: dict = {"schema": SCHEMA, "tables": {}, "columns": {}, "indexes": {}, "fks": {}}
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT CURDATE() AS d")
            out["generated"] = str(cur.fetchone()["d"])

            cur.execute(
                """
                SELECT TABLE_NAME AS name, TABLE_ROWS AS rows_est,
                       (DATA_LENGTH + INDEX_LENGTH) AS bytes, ENGINE AS engine,
                       TABLE_COMMENT AS comment
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                """,
                (SCHEMA,),
            )
            for r in cur.fetchall():
                out["tables"][r["name"]] = {
                    "rows": int(r["rows_est"] or 0),
                    "bytes": int(r["bytes"] or 0),
                    "engine": r["engine"],
                    "comment": r["comment"] or "",
                }

            # Detail only for Tier A — keeps the snapshot small.
            cur.execute(
                """
                SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col, COLUMN_TYPE AS typ,
                       IS_NULLABLE AS nullable, COLUMN_KEY AS ckey, EXTRA AS extra,
                       GENERATION_EXPRESSION AS gen, COLUMN_COMMENT AS comment
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (SCHEMA, TIER_A),
            )
            for r in cur.fetchall():
                out["columns"].setdefault(r["tbl"], []).append(
                    {k: (r[k] or "") for k in ("col", "typ", "nullable", "ckey", "extra", "gen", "comment")}
                )

            cur.execute(
                """
                SELECT TABLE_NAME AS tbl, INDEX_NAME AS idx, NON_UNIQUE AS non_unique,
                       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN %s
                GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE
                """,
                (SCHEMA, TIER_A),
            )
            for r in cur.fetchall():
                out["indexes"].setdefault(r["tbl"], []).append(
                    {"name": r["idx"], "unique": not r["non_unique"], "cols": r["cols"]}
                )

            cur.execute(
                """
                SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col,
                       REFERENCED_TABLE_NAME AS ref_tbl, REFERENCED_COLUMN_NAME AS ref_col
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN %s
                  AND REFERENCED_TABLE_NAME IS NOT NULL
                """,
                (SCHEMA, TIER_A),
            )
            for r in cur.fetchall():
                out["fks"].setdefault(r["tbl"], []).append(
                    {"col": r["col"], "ref": f"{r['ref_tbl']}.{r['ref_col']}"}
                )
    return out


# --------------------------------------------------------------------------
# render mode — host side, stdlib only
# --------------------------------------------------------------------------

def human_rows(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def human_size(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.1f}GB"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.0f}MB"
    return f"{b // 1024}KB"


def existing_manual(path: Path) -> str | None:
    if not path.exists():
        return None
    m = MANUAL_RE.search(path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def render_table(name: str, snap: dict) -> str:
    """Machine part of one Tier-A table file (manual block appended by caller)."""
    t = snap["tables"][name]
    cols = snap["columns"].get(name, [])
    idxs = snap["indexes"].get(name, [])
    fks = snap["fks"].get(name, [])

    pk = next((i["cols"] for i in idxs if i["name"] == "PRIMARY"), "—")
    head = (
        f"# {name} — ~{human_rows(t['rows'])} 行 / {human_size(t['bytes'])} / PK ({pk})\n\n"
        f"<!-- machine-generated {snap['generated']} · 改这区没用，重跑生成器会覆盖；业务注释写 manual 区 -->\n"
    )

    # Columns: compact tokens, ~5 per line.
    tokens = []
    for c in cols:
        tok = f"{c['col']} {c['typ']}"
        if c["ckey"] == "PRI":
            tok += " PK"
        if "auto_increment" in c["extra"]:
            tok += " AI"
        if c["gen"]:
            gen = re.sub(r"\s+", " ", c["gen"])[:40]
            tok += f" GEN({gen})"
        if c["comment"]:
            tok += f"（{c['comment'][:30]}）"
        tokens.append(tok)
    col_lines = []
    for i in range(0, len(tokens), 5):
        prefix = "列: " if i == 0 else "    "
        col_lines.append(prefix + " · ".join(tokens[i : i + 5]))

    idx_toks = [
        f"{'UQ ' if i['unique'] else ''}{i['name']}({i['cols']})"
        for i in idxs
        if i["name"] != "PRIMARY"
    ]
    idx_lines = []
    for i in range(0, len(idx_toks), 3):
        prefix = "索引: " if i == 0 else "      "
        idx_lines.append(prefix + " · ".join(idx_toks[i : i + 3]))
    if not idx_lines:
        idx_lines = ["索引: （仅 PK）"]

    parts = [head, "\n".join(col_lines), "", "\n".join(idx_lines)]
    if fks:
        parts += ["", "FK: " + " · ".join(f"{f['col']}→{f['ref']}" for f in fks)]
    return "\n".join(parts) + "\n"


def render(snap: dict, out_dir: Path) -> None:
    tables = snap["tables"]
    out_dir.mkdir(parents=True, exist_ok=True)
    tdir = out_dir / "tables"
    tdir.mkdir(exist_ok=True)

    missing_a = [n for n in TIER_A if n not in tables]
    if missing_a:
        print(f"WARN: Tier A tables not found in DB: {missing_a}", file=sys.stderr)

    # ---- per-table files (Tier A) ----
    for name in TIER_A:
        if name not in tables:
            continue
        path = tdir / f"{name}.md"
        manual = existing_manual(path) or MANUAL_PLACEHOLDER
        body = render_table(name, snap)
        path.write_text(
            f"{body}\n<!-- manual -->\n{manual}<!-- /manual -->\n", encoding="utf-8"
        )

    # ---- tiering for the index ----
    tier_a = [n for n in TIER_A if n in tables]
    noise, tier_b, tier_c = [], [], []
    for name, t in sorted(tables.items(), key=lambda kv: -kv[1]["bytes"]):
        if name in TIER_A:
            continue
        if NOISE_RE.search(name) or t["rows"] == 0:
            noise.append(name)
        elif t["bytes"] > TIER_B_MIN_BYTES:
            tier_b.append(name)
        else:
            tier_c.append(name)

    lines = [
        f"# fxbackoffice 表目录（{len(tables)} 张 · 生成 {snap['generated']} · 源 slave information_schema）",
        "",
        "> 行数是 InnoDB 估算值。用法：在这里定位表 → Tier A 再读 `tables/<table>.md` 拿列/索引/业务注释。",
        "> 通用约定（CEN÷100、UTC+3、demo/员工过滤、loginSid）见 SKILL.md。",
        "",
        f"## Tier A — 代码在用，有单表详情（{len(tier_a)} 张）",
        "",
        "| 表 | 行数 | 大小 | 用途 |",
        "|---|---|---|---|",
    ]
    for n in sorted(tier_a, key=lambda x: -tables[x]["bytes"]):
        t = tables[n]
        lines.append(
            f"| [{n}](tables/{n}.md) | {human_rows(t['rows'])} | {human_size(t['bytes'])} "
            f"| {PURPOSE.get(n, t['comment'])} |"
        )

    lines += [
        "",
        f"## Tier B — 大表（>10MB）但代码未引用（{len(tier_b)} 张）⚠ 别全表扫",
        "",
    ]
    for n in tier_b:
        t = tables[n]
        desc = PURPOSE.get(n) or t["comment"]
        lines.append(
            f"- `{n}` ~{human_rows(t['rows'])} 行 / {human_size(t['bytes'])}"
            + (f" — {desc}" if desc else "")
        )

    # Tier C grouped by prefix (first 2 tokens, fallback 1). The index keeps
    # only group names + counts; full member enumeration goes to _longtail.md
    # so the index stays small (staged loading).
    groups: dict[str, list[str]] = {}
    for n in tier_c:
        parts = n.split("_")
        key = "_".join(parts[:2]) if len(parts) > 2 else parts[0]
        groups.setdefault(key, []).append(n)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    singles = sorted(n for k, v in groups.items() if len(v) == 1 for n in v)
    lines += [
        "",
        f"## Tier C — 长尾小表（{len(tier_c)} 张）+ 忽略表（{len(noise)} 张 tmp/备份/空表）",
        "",
        "前缀分组: " + " · ".join(f"`{k}_*`×{len(multi[k])}" for k in sorted(multi)),
        "",
        f"另有单表 {len(singles)} 张。全名清单见 `_longtail.md`（很少需要加载——"
        "确认某表是否存在/查归属时才读）；表结构现场 `SHOW CREATE TABLE`。",
        "",
    ]

    (out_dir / "_index.md").write_text("\n".join(lines), encoding="utf-8")

    lt = [
        f"# fxbackoffice 长尾表全名清单（生成 {snap['generated']}）",
        "",
        f"## Tier C（{len(tier_c)} 张，<10MB 且代码未引用）",
        "",
    ]
    for key in sorted(multi):
        lt.append(f"- `{key}_*`（{len(multi[key])} 张）: " + " · ".join(sorted(multi[key])))
    if singles:
        lt.append("- 单表: " + " · ".join(singles))
    lt += [
        "",
        f"## 忽略 — tmp/备份/空表（{len(noise)} 张）",
        "",
        " · ".join(sorted(noise)) or "（无）",
        "",
    ]
    (out_dir / "_longtail.md").write_text("\n".join(lt), encoding="utf-8")
    print(
        f"OK: {len(tier_a)} Tier A files + _index.md "
        f"(A={len(tier_a)} B={len(tier_b)} C={len(tier_c)} ignore={len(noise)}) -> {out_dir}",
        file=sys.stderr,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("fetch", help="query information_schema, print JSON (run in container)")
    rp = sub.add_parser("render", help="JSON -> markdown (run on host, stdlib only)")
    rp.add_argument("--json", required=True, help="snapshot file from fetch mode")
    rp.add_argument("--out", required=True, help="output dir (the skill's fxbackoffice/)")
    args = ap.parse_args()

    if args.mode == "fetch":
        json.dump(fetch(), sys.stdout, ensure_ascii=False)
    else:
        render(json.loads(Path(args.json).read_text(encoding="utf-8")), Path(args.out))


if __name__ == "__main__":
    main()
