"""Tests for OPT-0042 hedge-open mail alert dispatcher.

Covers:
- Condition evaluation (A large-volume / B paired-orders) and their
  standard-lot boundaries. Lots arrive ALREADY normalised (detection scales
  CEN accounts ÷100, keyed on the currency authority); this layer must not
  re-scale off the symbol name. Cent normalisation itself is pinned in
  test_cen_lots_normalization.py.
- One digest per tick merging multiple accounts
- Per-login 30-min cooldown: defer, then merge into the next digest
- Outbox at-least-once: failed send is retried on the next tick, without
  composing a duplicate digest; retries are capped (terminal 'dead') and
  cross-process safe (claim before send, stale-claim requeue)
- Cursor advance (and holdback during cooldown deferral); a MISSING cursor
  row initializes at the alert_events high-water mark (no backlog replay)
- Subscription rule_ids narrowing of the module's rule band
- Seed subscription contents
- test-send rendering path (most-recent match + fallback)

⚠ Seed timestamps are ALWAYS relative to `datetime.now()` (OPT-0041
date-rot lesson): `append_scan_and_events` purges rows older than the
30-day retention window, so hardcoded dates would silently delete the
fixtures and the assertions would rot.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from app.core import risk_monitor_db as rm_db
from app.services import alert_mail_dispatcher as amd


# ── Fixtures / helpers ─────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
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
    server: str = "MT5",
    equity: float = -44696.65,
    net_deposit_hist: float | None = 139.89,
) -> dict:
    """A realistic hedge-open alert dict shaped like scan_hedge_open output."""
    at = at or NOW
    iso = _iso(at)
    return {
        "rule_id": 91,
        "rule_label": "Rule 1 — 默认对冲检测",
        "server": server,
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
    """Persist through the real write path; return the new alert_events.id."""
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


SEED_CONDITIONS = {
    "any": [
        {"type": "min_matched_lots_std", "min_matched_lots_std": 3.0},
        {
            "type": "paired_orders",
            "min_orders_per_side": 3,
            "min_matched_lots_std": 0.5,
        },
    ]
}


class MailCapture:
    """send_fn stub recording every send; optionally failing N times."""

    def __init__(self, fail_times: int = 0):
        self.sent: list[dict] = []
        self.calls = 0
        self.fail_times = fail_times

    def __call__(self, *, subject: str, body: str, to: str, cc=None) -> None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated SMTP failure")
        self.sent.append({"subject": subject, "body": body, "to": to, "cc": cc})


# ── Condition evaluation ───────────────────────────────────

def test_condition_a_hits_at_exact_boundary():
    alert = _hedge_alert(buy_lots=3.0, sell_lots=3.0, buy_count=1, sell_count=1)
    match = amd.evaluate_conditions(alert, SEED_CONDITIONS)
    assert match is not None
    assert match["matched_lots_std"] == pytest.approx(3.0)
    assert any(l.startswith("A") for l in match["labels"])


def test_condition_a_below_boundary_no_hit():
    alert = _hedge_alert(buy_lots=2.99, sell_lots=2.99, buy_count=1, sell_count=1)
    assert amd.evaluate_conditions(alert, SEED_CONDITIONS) is None


def test_condition_a_uses_min_side():
    # Asymmetric sides: matched volume = min(buy, sell) = 2.9 → no hit.
    alert = _hedge_alert(buy_lots=2.9, sell_lots=500.0, buy_count=1, sell_count=1)
    assert amd.evaluate_conditions(alert, SEED_CONDITIONS) is None


def test_cent_account_dust_below_condition_a():
    # A cent account's 15 raw lots reach us already normalised to 0.15 std
    # (detection ÷100) — the classic false positive stays dead here.
    alert = _hedge_alert(
        symbol="XAUUSD.cent", buy_lots=0.15, sell_lots=0.15,
        buy_count=1, sell_count=1,
    )
    assert amd.evaluate_conditions(alert, SEED_CONDITIONS) is None


def test_condition_a_boundary_hits():
    # Exactly 3 std matched (a cent account's raw 300) → A hits.
    alert = _hedge_alert(
        symbol="XAUUSD.cent", buy_lots=3.0, sell_lots=3.0,
        buy_count=1, sell_count=1,
    )
    match = amd.evaluate_conditions(alert, SEED_CONDITIONS)
    assert match is not None
    assert match["matched_lots_std"] == pytest.approx(3.0)


def test_symbol_suffix_does_not_affect_evaluation():
    """Cent-ness is an account property and is already applied upstream.

    The mail layer must not re-derive it from the symbol name — that hack
    both missed `.kcmc` (a second cent class) and double-divided `.cent`
    once detection started normalising. Identical std lots, identical verdict.
    """
    matches = [
        amd.evaluate_conditions(
            _hedge_alert(symbol=s, buy_lots=15.0, sell_lots=15.0,
                         buy_count=1, sell_count=1),
            SEED_CONDITIONS,
        )
        for s in ("XAUUSD.cent", "XAUUSD.CENT", "XAUUSD.kcmc", "XAUUSD")
    ]
    assert all(m is not None for m in matches)
    assert all(m["matched_lots_std"] == pytest.approx(15.0) for m in matches)


def test_condition_b_paired_orders():
    # 3+3 orders, 0.5 std matched → B hits even though A (3 lots) doesn't.
    alert = _hedge_alert(buy_lots=0.5, sell_lots=0.5, buy_count=3, sell_count=3)
    match = amd.evaluate_conditions(alert, SEED_CONDITIONS)
    assert match is not None
    assert any(l.startswith("B") for l in match["labels"])
    assert not any(l.startswith("A") for l in match["labels"])


def test_condition_b_below_order_count_no_hit():
    alert = _hedge_alert(buy_lots=0.5, sell_lots=0.5, buy_count=2, sell_count=3)
    assert amd.evaluate_conditions(alert, SEED_CONDITIONS) is None


def test_condition_b_lot_floor_boundary():
    # 3+3 orders but 0.25 std matched (a cent account's raw 25) < 0.5 → no hit.
    alert = _hedge_alert(
        symbol="EURUSD.cent", buy_lots=0.25, sell_lots=0.25,
        buy_count=3, sell_count=3,
    )
    assert amd.evaluate_conditions(alert, SEED_CONDITIONS) is None
    # 0.5 std matched (raw 50) → hits.
    alert = _hedge_alert(
        symbol="EURUSD.cent", buy_lots=0.5, sell_lots=0.5,
        buy_count=3, sell_count=3,
    )
    assert amd.evaluate_conditions(alert, SEED_CONDITIONS) is not None


def test_both_conditions_label_both():
    alert = _hedge_alert()  # the real case: huge volume AND 62 pairs
    match = amd.evaluate_conditions(alert, SEED_CONDITIONS)
    assert match is not None
    assert len(match["labels"]) == 2


def test_missing_detail_never_matches():
    alert = _hedge_alert()
    alert["buy_lots"] = None
    assert amd.evaluate_conditions(alert, SEED_CONDITIONS) is None


def test_unknown_condition_type_skipped():
    alert = _hedge_alert()
    conditions = {"any": [{"type": "some_v2_condition", "x": 1}]}
    assert amd.evaluate_conditions(alert, conditions) is None


# ── Seed subscription ──────────────────────────────────────

def test_seed_subscription_created(temp_db):
    subs = rm_db.load_mail_subscriptions(module="hedge_open")
    assert len(subs) == 1
    sub = subs[0]
    assert sub["mail_to"] == "kieran.xiang@kohleservices.com"
    assert sub["mode"] == "realtime"
    assert sub["cooldown_min"] == 30
    assert sub["enabled"] is True
    # Conditions parse into the exact A OR B tree the dispatcher evaluates.
    types = [c["type"] for c in sub["conditions"]["any"]]
    assert types == ["min_matched_lots_std", "paired_orders"]


def test_seed_subscription_not_duplicated_on_reinit(temp_db):
    rm_db.init_risk_monitor_db()
    assert len(rm_db.load_mail_subscriptions(module="hedge_open")) == 1


# ── Digest compose + cursor ────────────────────────────────

def test_one_digest_merges_multiple_accounts(temp_db):
    id1 = _insert(_hedge_alert(login=60011332))
    id2 = _insert(_hedge_alert(login=60011333))
    mail = MailCapture()

    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)

    assert summary["composed"] == 1 and summary["sent"] == 1
    assert len(mail.sent) == 1
    body = mail.sent[0]["body"]
    assert "60011332" in body and "60011333" in body
    assert mail.sent[0]["to"] == "kieran.xiang@kohleservices.com"
    assert "[Risk Alert]" in mail.sent[0]["subject"]
    # Sibling section: each account lists the other as a same-day sibling.
    assert "5-60011333" in body and "5-60011332" in body
    # Outbox row sent + stamped; cursor at the max alert id.
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    rows = rm_db.get_mail_outbox_rows(sub_id, ("sent",))
    assert len(rows) == 1
    assert rows[0]["notified_at"] is not None
    assert rm_db.get_mail_dispatch_cursor(sub_id) == max(id1, id2)


def test_second_tick_sends_nothing_new(temp_db):
    _insert(_hedge_alert())
    mail = MailCapture()
    amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=mail)
    assert len(mail.sent) == 1


def test_non_matching_alert_advances_cursor_silently(temp_db):
    # 0.5-lot dust lock — the 98.7% noise class. No email, cursor advances.
    aid = _insert(_hedge_alert(buy_lots=0.5, sell_lots=0.5, buy_count=1, sell_count=1))
    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 0
    assert mail.sent == []
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    assert rm_db.get_mail_dispatch_cursor(sub_id) == aid


def test_body_states_trigger_conditions(temp_db):
    # The digest must spell out the subscription's current thresholds so
    # recipients can judge the alert without opening the UI.
    _insert(_hedge_alert())
    mail = MailCapture()
    amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    body = mail.sent[0]["body"]
    assert "Trigger conditions (any one triggers):" in body
    assert "A - large hedge volume: matched lots &gt;= 3 std" in body
    assert (
        "B - scripted paired opens: &gt;= 3 orders each side "
        "and matched lots &gt;= 0.5 std"
    ) in body


def test_describe_conditions_shapes():
    # Legacy v1 tree → OR note + one line per condition.
    note, lines = amd.describe_conditions(SEED_CONDITIONS)
    assert note == "any one triggers"
    assert lines == [
        "A - large hedge volume: matched lots >= 3 std",
        "B - scripted paired opens: >= 3 orders each side "
        "and matched lots >= 0.5 std",
    ]
    # Generic tree → logic note + field/op/value lines.
    note, lines = amd.describe_conditions(
        {"logic": "and", "conditions": [
            {"field": "matched_lots_std", "op": ">=", "value": 2},
        ]}
    )
    assert note == "all must hold"
    assert lines == ["A - matched_lots_std >= 2"]
    # Empty tree → mail-everything wording, no join note.
    note, lines = amd.describe_conditions({})
    assert note == ""
    assert lines == ["(no conditions - every alert of this module is mailed)"]


def test_negative_equity_highlighted_in_body(temp_db):
    _insert(_hedge_alert(equity=-44696.65))
    mail = MailCapture()
    amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    body = mail.sent[0]["body"]
    assert "-44,696.65" in body
    assert "#c0392b" in body  # negative highlight color present


# ── Cooldown: defer then merge ─────────────────────────────

def test_cooldown_defers_same_login(temp_db):
    _insert(_hedge_alert(login=60011332), scanned_at=NOW)
    mail = MailCapture()
    amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert len(mail.sent) == 1

    # Same login hits again 5 minutes later — inside the 30-min cooldown.
    id2 = _insert(
        _hedge_alert(login=60011332, at=NOW + timedelta(minutes=5)),
        scanned_at=NOW + timedelta(minutes=5),
    )
    summary = amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=mail)
    assert summary["deferred"] == 1
    assert len(mail.sent) == 1  # no second email
    # Cursor held BELOW the deferred hit so it is re-pulled next tick.
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    assert rm_db.get_mail_dispatch_cursor(sub_id) < id2


def test_cooldown_hit_merges_into_next_digest(temp_db):
    _insert(_hedge_alert(login=60011332), scanned_at=NOW)
    mail = MailCapture()
    amd.dispatch_alert_mails(now=NOW, send_fn=mail)

    # Cooled repeat of the same login...
    _insert(
        _hedge_alert(login=60011332, at=NOW + timedelta(minutes=5)),
        scanned_at=NOW + timedelta(minutes=5),
    )
    amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=mail)
    assert len(mail.sent) == 1

    # ...then a DIFFERENT login fires → one digest carrying BOTH.
    _insert(
        _hedge_alert(login=60011333, at=NOW + timedelta(minutes=10)),
        scanned_at=NOW + timedelta(minutes=10),
    )
    summary = amd.dispatch_alert_mails(now=NOW + timedelta(minutes=10), send_fn=mail)
    assert summary["composed"] == 1
    assert len(mail.sent) == 2
    body = mail.sent[1]["body"]
    assert "60011332" in body and "60011333" in body
    assert "2 hedge-open alert(s)" in body


def test_cooldown_expiry_allows_new_digest(temp_db):
    _insert(_hedge_alert(login=60011332), scanned_at=NOW)
    mail = MailCapture()
    amd.dispatch_alert_mails(now=NOW, send_fn=mail)

    later = NOW + timedelta(minutes=40)  # past the 30-min cooldown
    _insert(_hedge_alert(login=60011332, at=later), scanned_at=later)
    summary = amd.dispatch_alert_mails(now=later, send_fn=mail)
    assert summary["composed"] == 1
    assert len(mail.sent) == 2


def test_digested_alert_not_reincluded_after_holdback(temp_db):
    """A cooled hit merged into a digest must not appear in a third email."""
    _insert(_hedge_alert(login=60011332), scanned_at=NOW)
    mail = MailCapture()
    amd.dispatch_alert_mails(now=NOW, send_fn=mail)

    id2 = _insert(
        _hedge_alert(login=60011332, at=NOW + timedelta(minutes=5)),
        scanned_at=NOW + timedelta(minutes=5),
    )
    amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=mail)
    _insert(
        _hedge_alert(login=60011333, at=NOW + timedelta(minutes=10)),
        scanned_at=NOW + timedelta(minutes=10),
    )
    amd.dispatch_alert_mails(now=NOW + timedelta(minutes=10), send_fn=mail)
    assert len(mail.sent) == 2

    # Another fresh login later: id2 was already digested and must not repeat.
    _insert(
        _hedge_alert(login=60011334, at=NOW + timedelta(minutes=50)),
        scanned_at=NOW + timedelta(minutes=50),
    )
    amd.dispatch_alert_mails(now=NOW + timedelta(minutes=50), send_fn=mail)
    assert len(mail.sent) == 3
    import json
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    last_row = rm_db.get_mail_outbox_rows(sub_id, ("sent",), limit=50)[-1]
    assert id2 not in json.loads(last_row["alert_ids_json"])


# ── rule_ids narrowing ─────────────────────────────────────

def _set_seed_rule_ids(value: str | None) -> None:
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE mail_subscriptions SET rule_ids = ? WHERE module = 'hedge_open'",
            (value,),
        )


def test_rule_ids_narrowing_excludes_other_rules(temp_db):
    # Operator narrows the seed row to rule 92 only: a rule-91 alert must
    # NOT be emailed, but the cursor still advances over it.
    _set_seed_rule_ids("[92]")
    aid = _insert(_hedge_alert())  # rule_id 91
    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 0
    assert mail.sent == []
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    assert rm_db.get_mail_dispatch_cursor(sub_id) == aid


def test_rule_ids_narrowing_includes_listed_rule(temp_db):
    _set_seed_rule_ids("[91]")
    _insert(_hedge_alert())  # rule_id 91
    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 1
    assert len(mail.sent) == 1


def test_rule_ids_invalid_json_falls_back_to_whole_band(temp_db):
    # Fail-open: a fat-fingered rule_ids must not silently drop all mail.
    _set_seed_rule_ids('{"not": "a list"}')
    _insert(_hedge_alert())
    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 1


def test_allowed_rule_ids_null_means_whole_band():
    assert amd.allowed_rule_ids({"id": 1, "rule_ids": None}) is None
    assert amd.allowed_rule_ids({"id": 1, "rule_ids": [91, 92]}) == {91, 92}


# ── Outbox at-least-once retry ─────────────────────────────

def test_outbox_retry_after_smtp_failure(temp_db):
    _insert(_hedge_alert())
    failing = MailCapture(fail_times=1)
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=failing)
    assert summary["failed"] == 1 and summary["sent"] == 0
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    rows = rm_db.get_mail_outbox_rows(sub_id, ("failed",))
    assert len(rows) == 1
    assert "simulated SMTP failure" in (rows[0]["error"] or "")

    # Next tick: no new alerts, but the failed row is retried and sent.
    summary = amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=failing)
    assert summary["retried"] == 1
    assert len(failing.sent) == 1
    rows = rm_db.get_mail_outbox_rows(sub_id, ("sent",))
    assert len(rows) == 1 and rows[0]["notified_at"] is not None
    # No duplicate compose happened.
    assert rm_db.get_mail_outbox_rows(sub_id, ("pending", "failed")) == []


def test_failed_send_does_not_block_cursor(temp_db):
    aid = _insert(_hedge_alert())
    failing = MailCapture(fail_times=10)
    amd.dispatch_alert_mails(now=NOW, send_fn=failing)
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    # Digest was composed → the alert is settled even though SMTP failed.
    assert rm_db.get_mail_dispatch_cursor(sub_id) == aid


# ── Retry cap / terminal 'dead' / cross-process claim ──────

def test_retry_capped_then_dead(temp_db):
    """A permanently failing digest stops retrying after the attempt cap."""
    _insert(_hedge_alert())
    failing = MailCapture(fail_times=999)
    for i in range(amd._MAX_SEND_ATTEMPTS + 3):
        amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5 * i), send_fn=failing)
    # Exactly cap attempts — the extra ticks made NO further SMTP calls.
    assert failing.calls == amd._MAX_SEND_ATTEMPTS
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    dead = rm_db.get_mail_outbox_rows(sub_id, ("dead",))
    assert len(dead) == 1
    assert dead[0]["attempts"] == amd._MAX_SEND_ATTEMPTS
    assert rm_db.get_mail_outbox_rows(sub_id, ("pending", "failed")) == []


def test_recipients_refused_goes_dead_immediately(temp_db):
    """A permanent SMTP recipient rejection must not burn the retry budget."""
    _insert(_hedge_alert())
    calls = {"n": 0}

    def refuse(*, subject: str, body: str, to: str, cc=None) -> None:
        calls["n"] += 1
        raise smtplib.SMTPRecipientsRefused({to: (550, b"mailbox unavailable")})

    amd.dispatch_alert_mails(now=NOW, send_fn=refuse)
    amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=refuse)
    assert calls["n"] == 1  # second tick did not retry the dead row
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    dead = rm_db.get_mail_outbox_rows(sub_id, ("dead",))
    assert len(dead) == 1
    assert "recipients refused" in (dead[0]["error"] or "")


def test_claimed_row_not_double_sent(temp_db):
    """A row claimed by another process (status='sending') is skipped."""
    _insert(_hedge_alert())
    failing = MailCapture(fail_times=1)
    amd.dispatch_alert_mails(now=NOW, send_fn=failing)  # composed, send failed
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    row_id = int(rm_db.get_mail_outbox_rows(sub_id, ("failed",))[0]["id"])

    # "Other process" wins the claim; a second claim on the same row loses.
    later = NOW + timedelta(minutes=5)
    assert rm_db.claim_mail_outbox_row(row_id, _iso(later)) is True
    assert rm_db.claim_mail_outbox_row(row_id, _iso(later)) is False

    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=later, send_fn=mail)
    assert mail.calls == 0  # fresh claim is respected, no duplicate send
    assert summary["retried"] == 0


def test_stale_sending_row_requeued_and_resent(temp_db):
    """A claim abandoned mid-send (process died) is requeued and retried."""
    _insert(_hedge_alert())
    failing = MailCapture(fail_times=1)
    amd.dispatch_alert_mails(now=NOW, send_fn=failing)  # composed, send failed
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    row_id = int(rm_db.get_mail_outbox_rows(sub_id, ("failed",))[0]["id"])

    later = NOW + timedelta(minutes=40)
    # Claimed 20 min before the next tick — beyond _STALE_SENDING_MIN.
    stale_claim = _iso(later - timedelta(minutes=amd._STALE_SENDING_MIN + 5))
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE mail_outbox SET status = 'sending', claimed_at = ? WHERE id = ?",
            (stale_claim, row_id),
        )

    summary = amd.dispatch_alert_mails(now=later, send_fn=failing)
    assert summary["retried"] == 1
    assert len(failing.sent) == 1
    rows = rm_db.get_mail_outbox_rows(sub_id, ("sent",))
    assert len(rows) == 1 and rows[0]["notified_at"] is not None


# ── Cursor plumbing ────────────────────────────────────────

def test_cursor_upsert_is_forward_only(temp_db):
    rm_db.update_mail_dispatch_cursor(1, 100)
    rm_db.update_mail_dispatch_cursor(1, 50)  # stale write must not regress
    assert rm_db.get_mail_dispatch_cursor(1) == 100
    rm_db.update_mail_dispatch_cursor(1, 150)
    assert rm_db.get_mail_dispatch_cursor(1) == 150


def test_cold_start_cursor_is_zero(temp_db):
    # Raw getter default for a row that does not exist. The DISPATCHER never
    # relies on this: it goes through ensure_mail_dispatch_cursor (below).
    assert rm_db.get_mail_dispatch_cursor(999) == 0


def _delete_cursor_rows() -> None:
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute("DELETE FROM mail_dispatch_cursor")


def test_missing_cursor_initialized_at_high_water_mark(temp_db):
    """Pre-fix prod state: subscription exists, cursor row missing, backlog
    of historical matches in alert_events. The first dispatch must NOT
    replay the backlog — it initializes the cursor to MAX(alert_events.id).
    """
    backlog_id = _insert(_hedge_alert(login=60011332))
    _delete_cursor_rows()

    mail = MailCapture()
    summary = amd.dispatch_alert_mails(now=NOW, send_fn=mail)
    assert summary["composed"] == 0
    assert mail.sent == []  # historical match NOT emailed as fresh
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    assert rm_db.get_mail_dispatch_cursor(sub_id) == backlog_id

    # An alert arriving AFTER initialization dispatches normally.
    new_id = _insert(
        _hedge_alert(login=60011333, at=NOW + timedelta(minutes=5)),
        scanned_at=NOW + timedelta(minutes=5),
    )
    summary = amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=mail)
    assert summary["composed"] == 1 and len(mail.sent) == 1
    import json
    row = rm_db.get_mail_outbox_rows(sub_id, ("sent",))[0]
    assert json.loads(row["alert_ids_json"]) == [new_id]


def test_init_backfills_missing_cursor_at_max(temp_db):
    """init_risk_monitor_db seeds a cursor row at MAX(alert_events.id) for
    any subscription without one (covers prod's already-seeded row)."""
    backlog_id = _insert(_hedge_alert())
    _delete_cursor_rows()
    rm_db.init_risk_monitor_db()
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    assert rm_db.get_mail_dispatch_cursor(sub_id) == backlog_id


def test_ensure_cursor_existing_row_untouched(temp_db):
    rm_db.update_mail_dispatch_cursor(1, 42)
    assert rm_db.ensure_mail_dispatch_cursor(1) == 42


def test_fast_forward_cursor_moves_to_high_water_mark(temp_db):
    """Re-enable helper: cursor jumps to MAX(alert_events.id), forward-only."""
    sub_id = rm_db.load_mail_subscriptions(module="hedge_open")[0]["id"]
    rm_db.update_mail_dispatch_cursor(sub_id, 1)
    aid = _insert(_hedge_alert())
    assert rm_db.fast_forward_mail_dispatch_cursor(sub_id) == aid
    assert rm_db.get_mail_dispatch_cursor(sub_id) == aid
    # Forward-only: a cursor already ahead never regresses.
    rm_db.update_mail_dispatch_cursor(sub_id, aid + 100)
    rm_db.fast_forward_mail_dispatch_cursor(sub_id)
    assert rm_db.get_mail_dispatch_cursor(sub_id) == aid + 100


# ── test-send path ─────────────────────────────────────────

def test_send_test_email_uses_most_recent_match(temp_db):
    _insert(_hedge_alert(buy_lots=0.5, sell_lots=0.5, buy_count=1, sell_count=1))
    aid = _insert(_hedge_alert(login=60011332))
    mail = MailCapture()
    result = amd.send_test_email(send_fn=mail)
    assert result["alert_id"] == aid
    assert result["used_fallback"] is False
    assert result["recipient"] == "kieran.xiang@kohleservices.com"
    assert mail.sent[0]["subject"].startswith("[TEST] ")
    assert "60011332" in mail.sent[0]["body"]


def test_send_test_email_falls_back_to_sample_fixture(temp_db):
    # Only a non-matching alert exists → the frozen in-code sample renders.
    _insert(_hedge_alert(buy_lots=0.5, sell_lots=0.5, buy_count=1, sell_count=1))
    mail = MailCapture()
    result = amd.send_test_email(send_fn=mail)
    assert result["used_fallback"] is True
    assert result["alert_id"] == amd.TEST_SEND_SAMPLE_ALERT["id"]
    assert len(mail.sent) == 1
    assert mail.sent[0]["subject"].startswith("[TEST] ")
    body = mail.sent[0]["body"]
    assert "60011332" in body and "NZDJPY" in body


def test_send_test_email_recipient_override(temp_db):
    _insert(_hedge_alert())
    mail = MailCapture()
    result = amd.send_test_email(recipient="someone@kcmtrade.com", send_fn=mail)
    assert result["recipient"] == "someone@kcmtrade.com"
    assert mail.sent[0]["to"] == "someone@kcmtrade.com"


def test_send_test_email_empty_table_uses_sample_fixture(temp_db):
    """Regression: the old fallback referenced live DB row 273504, which the
    30-day retention purge deletes — after that, test-send with no recent
    matching alert failed forever. The frozen in-code sample must keep
    test-send working even with a completely EMPTY alert_events table."""
    mail = MailCapture()
    result = amd.send_test_email(send_fn=mail)
    assert result["used_fallback"] is True
    assert result["alert_id"] == amd.TEST_SEND_SAMPLE_ALERT["id"]
    assert len(mail.sent) == 1
    assert mail.sent[0]["subject"].startswith("[TEST] ")
    # The sample mirrors the real 2026-07-03 case (62 buy + 62 sell NZDJPY).
    body = mail.sent[0]["body"]
    assert "62 buy + 62 sell" in body
    assert "-44,696.65" in body
    # The frozen sample dict itself was not mutated by the render.
    assert amd.TEST_SEND_SAMPLE_ALERT["buy_lots"] == 533.2


def test_send_test_email_without_sample_raises(temp_db):
    with pytest.raises(ValueError):
        amd.send_test_email(send_fn=MailCapture(), fallback_sample={})
