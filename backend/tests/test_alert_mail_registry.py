"""Tests for the OPT-0043 alert mail source registry + generalized dispatcher.

Covers:
- Registry lookup (get_source / list_source_descriptors / validators)
- Generic condition evaluation ({"logic","conditions"}): and/or, unknown
  field/op degrade to no-match, empty tree = match-all, .cent conversion
  via the matched_lots_std getter
- Dispatcher generalization: new-shape conditions dispatch end-to-end,
  legacy v1 {"any": [...]} trees still evaluate through the v1 path (the
  seed-subscription non-regression itself lives in
  test_alert_mail_dispatcher.py, which must stay green unchanged),
  unregistered-module subscriptions are skipped without crashing
- Digest mode: not composed on the realtime tick (outbox retries only),
  composed once daily inside the digest_time HKT window

⚠ Seed timestamps are ALWAYS relative to `datetime.now()` (OPT-0041
date-rot lesson) — append_scan_and_events purges rows older than the
30-day retention window.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core import risk_monitor_db as rm_db
from app.services import alert_mail_dispatcher as amd
from app.services.alert_mail import registry


# ── Fixtures / helpers (mirror test_alert_mail_dispatcher.py) ───────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    amd._digest_last_run.clear()
    return db_path


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _hedge_alert(
    *,
    login: int = 60011332,
    symbol: str = "NZDJPY",
    buy_count: int = 62,
    sell_count: int = 62,
    buy_lots: float = 533.2,
    sell_lots: float = 533.2,
    at: datetime | None = None,
    equity: float = -44696.65,
    net_deposit_hist: float | None = 139.89,
) -> dict:
    at = at or NOW
    iso = _iso(at)
    return {
        "rule_id": 91,
        "rule_label": "Rule 1 — 默认对冲检测",
        "server": "MT5",
        "login": login,
        "symbol": symbol,
        "order_count": buy_count + sell_count,
        "total_lots": round(buy_lots + sell_lots, 2),
        "first_open": iso,
        "last_open": iso,
        "equity": equity,
        "balance": equity,
        "group": "KCM\\5SD_P15L10",
        "orders": [],
        "currency": "USD",
        "zipcode": "111 90",
        "net_deposit_hist": net_deposit_hist,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_lots": buy_lots,
        "sell_lots": sell_lots,
        "window_start": iso,
        "window_end": iso,
    }


def _insert(alert: dict, scanned_at: datetime | None = None) -> int:
    rm_db.append_scan_and_events(
        scanned_at=_iso(scanned_at or NOW),
        scan_interval_min=5,
        accounts_scanned=1,
        suspicious_count=1,
        scan_time_ms=1,
        alerts=[alert],
    )
    rows = rm_db.fetch_recent_hedge_alerts(limit=1)
    assert rows, "insert through append_scan_and_events failed"
    return int(rows[0]["id"])


def _set_seed_conditions(tree: dict) -> None:
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE mail_subscriptions SET conditions_json = ? WHERE module = 'hedge_open'",
            (json.dumps(tree),),
        )


HEDGE = registry.MAIL_SOURCES["hedge_open"]


class MailCapture:
    def __init__(self):
        self.sent: list[dict] = []

    def __call__(self, *, subject: str, body: str, to: str, cc=None) -> None:
        self.sent.append({"subject": subject, "body": body, "to": to, "cc": cc})


# ── Registry lookup ────────────────────────────────────────

def test_get_source_known_module():
    src = registry.get_source("hedge_open")
    assert src is not None
    assert src["rule_id_range"] == (91, 100)
    assert callable(src["template_builder"])


def test_get_source_unknown_module_is_none():
    assert registry.get_source("fund_flow") is None


def test_list_source_descriptors_shape(temp_db):
    descriptors = registry.list_source_descriptors()
    assert len(descriptors) == 1
    d = descriptors[0]
    assert d["module"] == "hedge_open"
    assert d["rule_id_range"] == [91, 100]
    fields = {f["field"]: f for f in d["filterable_fields"]}
    assert set(fields) == {
        "matched_lots_std", "orders_per_side", "total_lots",
        "equity", "net_deposit_hist",
    }
    assert fields["orders_per_side"]["type"] == "int"
    assert fields["matched_lots_std"]["type"] == "float"
    # Rules come from the seeded hedge_open_rules row, band-clamped to 91.
    assert len(d["rules"]) == 1
    rule = d["rules"][0]
    assert rule["id"] == 91
    assert rule["name"] == "默认对冲检测"
    assert rule["enabled"] is True
    assert rule["params"]["window_sec"] == 3


def test_validate_module_raises_on_unknown():
    with pytest.raises(ValueError):
        registry.validate_module("nope")


def test_validate_rule_ids_band():
    registry.validate_rule_ids("hedge_open", None)
    registry.validate_rule_ids("hedge_open", [91, 100])
    with pytest.raises(ValueError):
        registry.validate_rule_ids("hedge_open", [90])
    with pytest.raises(ValueError):
        registry.validate_rule_ids("hedge_open", [101])


def test_validate_condition_fields():
    ok = {"logic": "and", "conditions": [{"field": "equity", "op": "<", "value": 0}]}
    registry.validate_condition_fields("hedge_open", ok)
    bad = {"logic": "and", "conditions": [{"field": "balance", "op": "<", "value": 0}]}
    with pytest.raises(ValueError):
        registry.validate_condition_fields("hedge_open", bad)


def test_coerce_condition_values_to_declared_types():
    """Regression: a raw API client sending string values ('100') stored them
    verbatim, and _compare's == branch string-compared '100' vs 100.0 —
    never matching. Values must be coerced to the registry-declared type."""
    tree = {
        "logic": "and",
        "conditions": [
            {"field": "equity", "op": "==", "value": "100"},       # float field
            {"field": "orders_per_side", "op": ">=", "value": "5"},  # int field
            {"field": "total_lots", "op": ">", "value": 7},          # int → float
        ],
    }
    registry.coerce_condition_values("hedge_open", tree)
    assert tree["conditions"][0]["value"] == 100.0
    assert isinstance(tree["conditions"][0]["value"], float)
    assert tree["conditions"][1]["value"] == 5
    assert isinstance(tree["conditions"][1]["value"], int)
    assert isinstance(tree["conditions"][2]["value"], float)


def test_coerce_condition_values_rejects_garbage():
    bad_float = {"logic": "and",
                 "conditions": [{"field": "equity", "op": "==", "value": "abc"}]}
    with pytest.raises(ValueError):
        registry.coerce_condition_values("hedge_open", bad_float)
    non_integer = {"logic": "and",
                   "conditions": [{"field": "orders_per_side", "op": "==", "value": "5.5"}]}
    with pytest.raises(ValueError):
        registry.coerce_condition_values("hedge_open", non_integer)
    non_finite = {"logic": "and",
                  "conditions": [{"field": "equity", "op": "==", "value": "nan"}]}
    with pytest.raises(ValueError):
        registry.coerce_condition_values("hedge_open", non_finite)


def test_coerced_string_value_matches_numeric_field():
    """End of the == bug: after coercion, equity == 100.0 matches an alert
    whose equity is exactly 100.0."""
    tree = {"logic": "and",
            "conditions": [{"field": "equity", "op": "==", "value": "100"}]}
    registry.coerce_condition_values("hedge_open", tree)
    alert = _hedge_alert(equity=100.0)
    assert amd.evaluate_generic_conditions(alert, tree, HEDGE) is not None
    alert2 = _hedge_alert(equity=100.5)
    assert amd.evaluate_generic_conditions(alert2, tree, HEDGE) is None


# ── Generic condition evaluation ───────────────────────────

def _tree(logic: str, *conds: dict) -> dict:
    return {"logic": logic, "conditions": list(conds)}


def test_generic_and_all_must_hold():
    alert = _hedge_alert(buy_lots=20.0, sell_lots=20.0, equity=-100.0)
    tree = _tree(
        "and",
        {"field": "matched_lots_std", "op": ">=", "value": 10},
        {"field": "equity", "op": "<", "value": 0},
    )
    match = amd.evaluate_generic_conditions(alert, tree, HEDGE)
    assert match is not None
    assert match["matched_lots_std"] == pytest.approx(20.0)
    assert len(match["labels"]) == 2

    # equity condition fails -> AND fails.
    alert2 = _hedge_alert(buy_lots=20.0, sell_lots=20.0, equity=100.0)
    assert amd.evaluate_generic_conditions(alert2, tree, HEDGE) is None


def test_generic_or_any_suffices():
    tree = _tree(
        "or",
        {"field": "matched_lots_std", "op": ">=", "value": 10},
        {"field": "orders_per_side", "op": ">=", "value": 5},
    )
    # Only the orders leg hits.
    alert = _hedge_alert(buy_lots=1.0, sell_lots=1.0, buy_count=5, sell_count=7)
    match = amd.evaluate_generic_conditions(alert, tree, HEDGE)
    assert match is not None
    assert len(match["labels"]) == 1
    assert "orders_per_side" in match["labels"][0]

    # Neither hits.
    alert2 = _hedge_alert(buy_lots=1.0, sell_lots=1.0, buy_count=2, sell_count=2)
    assert amd.evaluate_generic_conditions(alert2, tree, HEDGE) is None


def test_generic_empty_conditions_match_all():
    alert = _hedge_alert(buy_lots=0.01, sell_lots=0.01, buy_count=1, sell_count=1)
    assert amd.evaluate_generic_conditions(alert, None, HEDGE) is not None
    assert amd.evaluate_generic_conditions(alert, {}, HEDGE) is not None
    assert (
        amd.evaluate_generic_conditions(alert, {"logic": "or", "conditions": []}, HEDGE)
        is not None
    )


def test_generic_unknown_field_never_matches():
    alert = _hedge_alert()
    tree = _tree("and", {"field": "not_a_field", "op": ">=", "value": 0})
    assert amd.evaluate_generic_conditions(alert, tree, HEDGE) is None


def test_generic_unknown_op_never_matches():
    alert = _hedge_alert()
    tree = _tree("and", {"field": "matched_lots_std", "op": "!=", "value": 0})
    assert amd.evaluate_generic_conditions(alert, tree, HEDGE) is None


def test_generic_none_field_value_never_matches():
    alert = _hedge_alert(net_deposit_hist=None)
    tree = _tree("and", {"field": "net_deposit_hist", "op": "<", "value": 1e9})
    assert amd.evaluate_generic_conditions(alert, tree, HEDGE) is None


def test_generic_cent_conversion_applies():
    # 1500 cent-lots = 15 std -> >= 10 hits; raw 1500 would also hit, so
    # assert the boundary the other way: 900 cent-lots = 9 std -> no hit.
    tree = _tree("and", {"field": "matched_lots_std", "op": ">=", "value": 10})
    alert = _hedge_alert(symbol="XAUUSD.cent", buy_lots=900.0, sell_lots=900.0)
    assert amd.evaluate_generic_conditions(alert, tree, HEDGE) is None
    alert2 = _hedge_alert(symbol="XAUUSD.cent", buy_lots=1500.0, sell_lots=1500.0)
    assert amd.evaluate_generic_conditions(alert2, tree, HEDGE) is not None


def test_generic_missing_detail_never_matches():
    alert = _hedge_alert()
    alert["buy_lots"] = None  # match_context -> None
    assert amd.evaluate_generic_conditions(alert, None, HEDGE) is None


def test_evaluate_subscription_routes_legacy_tree_to_v1_path():
    alert = _hedge_alert(buy_lots=10.0, sell_lots=10.0, buy_count=1, sell_count=1)
    sub = {
        "conditions": {
            "any": [{"type": "min_matched_lots_std", "min_matched_lots_std": 10.0}]
        }
    }
    match = amd.evaluate_subscription_conditions(alert, sub, HEDGE)
    assert match is not None
    assert match["labels"][0].startswith("A - large hedge volume")


# ── Dispatcher generalization (new-shape conditions e2e) ───

def test_dispatch_with_new_shape_conditions(temp_db):
    _set_seed_conditions(
        _tree("or", {"field": "matched_lots_std", "op": ">=", "value": 10})
    )
    below = _insert(
        _hedge_alert(login=1111, buy_lots=9.9, sell_lots=9.9, buy_count=1, sell_count=1)
    )
    hit = _insert(_hedge_alert(login=2222, buy_lots=10.0, sell_lots=10.0))
    mail = MailCapture()

    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)

    assert summary["composed"] == 1 and summary["sent"] == 1
    assert len(mail.sent) == 1
    body = mail.sent[0]["body"]
    assert "2222" in body
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    row = rm_db.get_mail_outbox_rows(sub_id, ("sent",))[0]
    assert json.loads(row["alert_ids_json"]) == [hit]
    # Cursor advanced past BOTH alerts (the miss is settled silently).
    assert rm_db.get_mail_dispatch_cursor(sub_id) == max(below, hit)


def test_dispatch_new_shape_respects_rule_ids_narrowing(temp_db):
    _set_seed_conditions(
        _tree("or", {"field": "matched_lots_std", "op": ">=", "value": 10})
    )
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE mail_subscriptions SET rule_ids = '[92]' WHERE module = 'hedge_open'"
        )
    _insert(_hedge_alert())  # rule_id 91 — excluded by the narrowing
    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 0
    assert mail.sent == []


def test_unregistered_module_subscription_is_skipped(temp_db):
    rm_db.insert_mail_subscription({
        "name": "ghost", "module": "not_registered", "rule_ids": None,
        "conditions_json": "{}", "mail_to": "kieran.xiang@kohleservices.com",
        "mail_cc": None, "mode": "realtime", "cooldown_min": 0,
        "digest_time": None, "enabled": 1,
        "updated_at": _iso(NOW), "updated_by": "test",
    })
    _insert(_hedge_alert())
    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    # Seed hedge subscription still dispatched; ghost skipped, no crash.
    assert summary["composed"] == 1
    assert len(mail.sent) == 1


# ── Digest mode ────────────────────────────────────────────

def _create_digest_sub(digest_time: str) -> int:
    sub_id = rm_db.insert_mail_subscription({
        "name": "daily digest", "module": "hedge_open", "rule_ids": None,
        "conditions_json": json.dumps(
            _tree("or", {"field": "matched_lots_std", "op": ">=", "value": 10})
        ),
        "mail_to": "kieran.xiang@kohleservices.com", "mail_cc": None,
        "mode": "digest", "cooldown_min": 0, "digest_time": digest_time,
        "enabled": 1, "updated_at": _iso(NOW), "updated_by": "test",
    })
    # Cursor at 0 so the digest sees the fixture alerts (a real new
    # subscription starts at the high-water mark instead).
    rm_db.update_mail_dispatch_cursor(sub_id, 0)
    return sub_id


def test_realtime_tick_does_not_compose_digest_subs(temp_db):
    # Disable the seed realtime sub to isolate the digest one.
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute("UPDATE mail_subscriptions SET enabled = 0 WHERE module = 'hedge_open'")
    _create_digest_sub("03:00")
    _insert(_hedge_alert())
    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 0
    assert mail.sent == []


def test_digest_composes_inside_window_once_per_day(temp_db):
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute("UPDATE mail_subscriptions SET enabled = 0 WHERE module = 'hedge_open'")
    now_hkt = NOW.astimezone(amd.HKT)
    sub_id = _create_digest_sub(now_hkt.strftime("%H:%M"))
    _insert(_hedge_alert(login=3333))
    mail = MailCapture()

    summary = amd.dispatch_digest_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 1 and summary["sent"] == 1
    assert "3333" in mail.sent[0]["body"]

    # Second run the same day inside the window: once-per-day guard holds.
    _insert(_hedge_alert(login=4444, at=NOW + timedelta(minutes=1)),
            scanned_at=NOW + timedelta(minutes=1))
    summary = amd.dispatch_digest_mails(now=NOW + timedelta(minutes=2), send_fn=mail)
    assert summary["composed"] == 0
    assert len(mail.sent) == 1
    assert sub_id in amd._digest_last_run


def test_digest_not_due_outside_window(temp_db):
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute("UPDATE mail_subscriptions SET enabled = 0 WHERE module = 'hedge_open'")
    # digest_time one hour in the future (HKT) — never due now.
    later_hkt = (NOW + timedelta(hours=1)).astimezone(amd.HKT)
    _create_digest_sub(later_hkt.strftime("%H:%M"))
    _insert(_hedge_alert())
    mail = MailCapture()
    summary = amd.dispatch_digest_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 0
    assert mail.sent == []


def test_realtime_tick_retries_failed_digest_rows(temp_db):
    """A digest row that failed to send is retried on the realtime cadence."""
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute("UPDATE mail_subscriptions SET enabled = 0 WHERE module = 'hedge_open'")
    now_hkt = NOW.astimezone(amd.HKT)
    sub_id = _create_digest_sub(now_hkt.strftime("%H:%M"))
    _insert(_hedge_alert())

    fails = {"n": 1}

    def flaky(*, subject: str, body: str, to: str, cc=None) -> None:
        if fails["n"] > 0:
            fails["n"] -= 1
            raise RuntimeError("smtp down")

    amd.dispatch_digest_mails(now=NOW, send_fn=flaky)
    assert rm_db.get_mail_outbox_rows(sub_id, ("failed",))

    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=mail)
    assert summary["retried"] == 1
    assert rm_db.get_mail_outbox_rows(sub_id, ("sent",))


def test_digest_drains_backlog_beyond_one_fetch_batch(temp_db, monkeypatch):
    """All same-day alerts land in ONE daily digest even past the fetch batch.

    Regression (OPT-0043): the once-daily digest compose pulled a single
    default-limit batch, capping the cursor advance at 500 alerts/day and
    silently building a compounding backlog on bursty days.
    """
    monkeypatch.setattr(amd, "_DIGEST_FETCH_BATCH", 2)
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute("UPDATE mail_subscriptions SET enabled = 0 WHERE module = 'hedge_open'")
    now_hkt = NOW.astimezone(amd.HKT)
    sub_id = _create_digest_sub(now_hkt.strftime("%H:%M"))
    ids = [_insert(_hedge_alert(login=7000 + i)) for i in range(5)]

    mail = MailCapture()
    summary = amd.dispatch_digest_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 1 and summary["sent"] == 1
    rows = rm_db.get_mail_outbox_rows(sub_id, ("sent",))
    assert len(rows) == 1
    assert sorted(json.loads(rows[0]["alert_ids_json"])) == sorted(ids)
    # Cursor advanced past the WHOLE backlog, not just the first batch.
    assert rm_db.get_mail_dispatch_cursor(sub_id) == max(ids)


def test_digest_drain_respects_hard_cap(temp_db, monkeypatch):
    """The drain stops at _DIGEST_MAX_ALERTS; the rest rolls to tomorrow."""
    monkeypatch.setattr(amd, "_DIGEST_FETCH_BATCH", 2)
    monkeypatch.setattr(amd, "_DIGEST_MAX_ALERTS", 4)
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute("UPDATE mail_subscriptions SET enabled = 0 WHERE module = 'hedge_open'")
    now_hkt = NOW.astimezone(amd.HKT)
    sub_id = _create_digest_sub(now_hkt.strftime("%H:%M"))
    ids = [_insert(_hedge_alert(login=8000 + i)) for i in range(5)]

    mail = MailCapture()
    summary = amd.dispatch_digest_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 1
    rows = rm_db.get_mail_outbox_rows(sub_id, ("sent",))
    assert sorted(json.loads(rows[0]["alert_ids_json"])) == sorted(ids[:4])
    # Cursor stops at the cap so the 5th alert is tomorrow's digest.
    assert rm_db.get_mail_dispatch_cursor(sub_id) == ids[3]
