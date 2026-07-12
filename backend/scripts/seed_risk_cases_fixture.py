#!/usr/bin/env python3
"""
Seed deterministic FIXTURE cases into the risk_cases PG database (OPT-0047).

Purpose: the watchlist frontend can be developed / demoed before the real
signal + baseline pipeline has produced data. All fixture rows are clearly
marked (tag "fixture", user_id band 990000001+) and fully removable.

Coverage matrix (exercises every frontend edge the page must handle):
    - 8 cases with 35 days of metric snapshots  → Δ1 and Δ30 both available
    - 2 cases with 2 days of snapshots          → Δ1 available, Δ30 = "—"
    - 1 case with 1 day of snapshots            → both Δ = "—"
    - 1 case with NO snapshot at all            → every metric column empty
    - one 17-account client (case 127582 analog, AC1 many-account merge)
    - mixed states: mostly watching + one disposed / whitelisted / archived
      (V2 pipeline only writes watching; these exercise the state filter)

Usage:
    cd backend
    .venv/bin/python scripts/seed_risk_cases_fixture.py            # dry-run
    .venv/bin/python scripts/seed_risk_cases_fixture.py --apply    # insert
    .venv/bin/python scripts/seed_risk_cases_fixture.py --remove   # delete all fixtures
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT / ".env")

from app.core.risk_cases_pg import init_risk_cases_pg, risk_cases_conn  # noqa: E402

FIXTURE_TAG = "fixture"
FIXTURE_UID_BASE = 990_000_001
RNG_SEED = 47  # deterministic content — reruns produce identical rows

_SERVER_BY_SID = {1: "MT4_Live", 5: "MT5", 6: "MT4_Live2"}
_SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "US30"]
_COUNTRIES = ["CN", "TH", "VN", "MY", "AE", "GB"]
_NAMES = [
    "Fixture Trader A", "Fixture Trader B", "Fixture Trader C",
    "Fixture Trader D", "Fixture Trader E", "Fixture Trader F",
    "Fixture Trader G", "Fixture Trader H", "Fixture Trader I",
    "Fixture Trader J", "Fixture Trader K", "Fixture Trader L",
]


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def build_fixture_plan(today: date | None = None) -> list[dict]:
    """Pure builder (unit-testable): returns one dict per fixture case."""
    rng = random.Random(RNG_SEED)
    today = today or date.today()
    plan: list[dict] = []

    # (snapshot_days, account_count, state)
    shapes = [
        (35, 17, "watching"),   # AC1 analog: 17 accounts, full Δ history
        (35, 3, "watching"),
        (35, 1, "watching"),
        (35, 5, "watching"),
        (35, 2, "watching"),
        (35, 4, "watching"),
        (35, 1, "disposed"),
        (35, 2, "whitelisted"),
        (2, 3, "watching"),
        (2, 1, "watching"),
        (1, 2, "watching"),
        (0, 1, "archived"),
    ]
    for i, (snap_days, n_accounts, state) in enumerate(shapes):
        uid = FIXTURE_UID_BASE + i
        rebate_30 = round(rng.uniform(500, 25_000), 2)
        pl_30 = round(rng.uniform(-0.9, 0.6) * rebate_30, 2)
        base_hold_days = round(rng.uniform(0.01, 3.0), 3)

        entities = []
        for j in range(n_accounts):
            sid = rng.choice(list(_SERVER_BY_SID))
            login = 8_000_000 + uid % 1000 * 100 + j
            entities.append(
                {
                    "server": _SERVER_BY_SID[sid],
                    "login": login,
                    "login_sid": f"{sid}-{login}",
                    "family": "",
                }
            )

        # Condensed signal timeline: one rule-121 signal every ~2 days.
        signals = []
        n_signals = max(1, snap_days // 2) if snap_days else 1
        for k in range(n_signals):
            ts = datetime.now(timezone.utc) - timedelta(days=2 * (n_signals - 1 - k))
            signals.append(
                {
                    "event_id": None,
                    "scanned_at": _iso_utc(ts),
                    "rule_id": 121,
                    "rule_label": "Rebate Arbitrage · 返佣套利",
                    "window_date": (today - timedelta(days=2 * (n_signals - 1 - k))).isoformat(),
                    "rebate_30d": round(rebate_30 * rng.uniform(0.8, 1.0), 2),
                    "total_pl_30d": pl_30,
                    "combined_30d": round(rebate_30 + pl_30, 2),
                    "ratio_5m": round(rng.uniform(0.3, 0.95), 4),
                    "ratio_10m": round(rng.uniform(0.5, 0.99), 4),
                    "hold_geo_mean_sec": round(rng.uniform(30, 900), 1),
                    "trading_net_deposit": round(rng.uniform(-50_000, 10_000), 2),
                    "ib_withdrawal": round(rng.uniform(-30_000, 0), 2),
                    "contributing_login_sids": ",".join(
                        e["login_sid"] for e in entities
                    ),
                    "contributing_account_count": n_accounts,
                    "wallet_login_sids": f"2-{10_000 + i}:{rebate_30:.2f}",
                }
            )

        # Daily metric snapshots, newest = today; hold time drifts slightly
        # so Δ1/Δ30 are non-zero but plausible.
        snapshots = []
        for d in range(snap_days):
            md = today - timedelta(days=snap_days - 1 - d)
            drift = (d - snap_days / 2) * 0.002
            lots_30 = round(rng.uniform(50, 900), 2)
            profit_30 = round(pl_30 * rng.uniform(0.9, 1.1), 2)
            reb_30 = round(rebate_30 * rng.uniform(0.9, 1.1), 2)
            snapshots.append(
                {
                    "metric_date": md,
                    "orders_7d": rng.randint(5, 200),
                    "orders_30d": rng.randint(200, 900),
                    "orders_90d": rng.randint(900, 2500),
                    "orders_all": rng.randint(2500, 9000),
                    "lots_7d": round(lots_30 / 4, 2),
                    "lots_30d": lots_30,
                    "lots_90d": round(lots_30 * 2.7, 2),
                    "lots_all": round(lots_30 * 8.1, 2),
                    "avg_hold_days_30d": round(base_hold_days + drift, 4),
                    "ratio_5m_30d": round(rng.uniform(0.3, 0.95), 4),
                    "ratio_10m_30d": round(rng.uniform(0.5, 0.99), 4),
                    "trading_net_deposit": round(rng.uniform(-50_000, 10_000), 2),
                    "ib_withdrawal": round(rng.uniform(-30_000, 0), 2),
                    "profit_7d": round(profit_30 / 4, 2),
                    "profit_30d": profit_30,
                    "profit_all": round(profit_30 * 3.2, 2),
                    "rebate_7d": round(reb_30 / 4, 2),
                    "rebate_30d": reb_30,
                    "rebate_all": round(reb_30 * 4.5, 2),
                    "combined_30d": round(profit_30 + reb_30, 2),
                    "top_symbol_1": rng.choice(_SYMBOLS),
                    "top_symbol_1_ratio": round(rng.uniform(0.4, 0.9), 4),
                    "top_symbol_2": rng.choice(_SYMBOLS),
                    "top_symbol_2_ratio": round(rng.uniform(0.05, 0.3), 4),
                    "equity": round(rng.uniform(100, 80_000), 2),
                    "account_count": n_accounts,
                }
            )

        plan.append(
            {
                "user_id": uid,
                "state": state,
                "tags": ["rebate_arb", FIXTURE_TAG],
                "user_name": _NAMES[i],
                "country": rng.choice(_COUNTRIES),
                "signal_count": n_signals,
                "first_signal_at": signals[0]["scanned_at"],
                "last_signal_at": signals[-1]["scanned_at"],
                "signals": signals,
                "entities": entities,
                "snapshots": snapshots,
            }
        )
    return plan


def apply_plan(plan: list[dict]) -> None:
    with risk_cases_conn() as conn:
        with conn.cursor() as cur:
            for case in plan:
                cur.execute(
                    """
                    INSERT INTO risk_cases
                        (user_id, state, tags, signal_count, first_signal_at,
                         last_signal_at, signal_timeline, user_name, country)
                    VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        state = EXCLUDED.state,
                        tags = EXCLUDED.tags,
                        signal_count = EXCLUDED.signal_count,
                        first_signal_at = EXCLUDED.first_signal_at,
                        last_signal_at = EXCLUDED.last_signal_at,
                        signal_timeline = EXCLUDED.signal_timeline,
                        user_name = EXCLUDED.user_name,
                        country = EXCLUDED.country,
                        updated_at = now()
                    """,
                    (
                        case["user_id"], case["state"], json.dumps(case["tags"]),
                        case["signal_count"], case["first_signal_at"],
                        case["last_signal_at"], json.dumps(case["signals"]),
                        case["user_name"], case["country"],
                    ),
                )
                for e in case["entities"]:
                    cur.execute(
                        """
                        INSERT INTO case_entities
                            (user_id, server, login, family, login_sid)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT ON CONSTRAINT uq_case_entities
                        DO UPDATE SET last_seen_at = now()
                        """,
                        (
                            case["user_id"], e["server"], e["login"],
                            e["family"], e["login_sid"],
                        ),
                    )
                for s in case["snapshots"]:
                    cols = list(s.keys())
                    placeholders = ", ".join(["%s"] * (1 + len(cols)))
                    updates = ", ".join(
                        f"{c} = EXCLUDED.{c}" for c in cols if c != "metric_date"
                    )
                    cur.execute(
                        f"""
                        INSERT INTO case_metrics_daily
                            (user_id, {', '.join(cols)})
                        VALUES ({placeholders})
                        ON CONFLICT ON CONSTRAINT uq_case_metrics_daily
                        DO UPDATE SET {updates}
                        """,
                        (case["user_id"], *[s[c] for c in cols]),
                    )


def remove_fixtures() -> None:
    with risk_cases_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM risk_cases WHERE tags ? %s", (FIXTURE_TAG,)
            )
            uids = [int(r["user_id"]) for r in cur.fetchall()]
            if not uids:
                print("No fixture cases found.")
                return
            # case_entities cascades; metrics/actions have no FK — delete
            # explicitly.
            cur.execute(
                "DELETE FROM case_metrics_daily WHERE user_id = ANY(%s)", (uids,)
            )
            cur.execute("DELETE FROM case_actions WHERE user_id = ANY(%s)", (uids,))
            cur.execute("DELETE FROM risk_cases WHERE user_id = ANY(%s)", (uids,))
            print(f"Removed {len(uids)} fixture case(s): {uids}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="insert fixtures")
    parser.add_argument("--remove", action="store_true", help="delete fixtures")
    args = parser.parse_args()

    if args.remove:
        remove_fixtures()
        return 0

    plan = build_fixture_plan()
    total_snaps = sum(len(c["snapshots"]) for c in plan)
    total_entities = sum(len(c["entities"]) for c in plan)
    print(
        f"Plan: {len(plan)} cases, {total_entities} entities, "
        f"{total_snaps} metric snapshots (uid {plan[0]['user_id']}..{plan[-1]['user_id']})"
    )
    if not args.apply:
        print("Dry-run only. Pass --apply to write, --remove to clean up.")
        return 0

    if not init_risk_cases_pg():
        print("ERROR: risk_cases PG unavailable")
        return 1
    apply_plan(plan)
    print("Fixtures applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
