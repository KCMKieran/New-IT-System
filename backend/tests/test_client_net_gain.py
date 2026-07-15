"""Tests for get_client_net_gain_map — the client-level operands behind the
risk-monitor 淨賺 column (2026-07 audit fix).

The audit found the column carried three biases; this map addresses two of
them at the data layer:

1. Level mismatch — `equity` was ONE account's snapshot while
   `net_deposit_hist` was the CLIENT's lifetime total, so every row of a
   multi-account client subtracted the client's whole net deposit from a
   single account's equity.
2. 'ib withdrawal' was folded into the deposit leg, so an IB-cum-trader's
   commission cash-outs read as withdrawn capital and inflated 淨賺.

(Bias 3 — the missing rebate leg — is deliberately NOT addressed here; the
column stays an approximation and says so in its tooltip.)

The three underlying queries are monkeypatched: this pins the mapping /
fail-open / null contracts, not the SQL.
"""

import app.services.account_enrichment as ae


def _patch(monkeypatch, *, userids, nd_split, equity):
    monkeypatch.setattr(ae, "get_user_id_map", lambda conn, alerts: userids)
    monkeypatch.setattr(ae, "query_net_deposit_split", lambda conn, u: nd_split)
    monkeypatch.setattr(ae, "query_equity_by_userid", lambda conn, u: equity)


_ALERTS = [{"server": "MT4_Live", "login": 1}, {"server": "MT4_Live", "login": 2}]


def test_siblings_of_one_client_get_identical_operands(monkeypatch):
    """The defining property of the fix: two accounts of the SAME client
    resolve to the same client-level pair, so 淨賺 agrees across their rows."""
    _patch(
        monkeypatch,
        userids={"1-1": 500, "1-2": 500},
        nd_split={500: {"trading_net_deposit": 5000.0, "ib_withdrawal": -1000.0}},
        equity={500: 20000.0},
    )
    out = ae.get_client_net_gain_map(object(), _ALERTS)
    assert out["1-1"] == out["1-2"] == {
        "client_equity": 20000.0,
        "client_trading_net_deposit": 5000.0,
    }
    # 淨賺 = 20000 - 5000 = 15000 for BOTH rows.
    assert out["1-1"]["client_equity"] - out["1-1"]["client_trading_net_deposit"] == 15000.0


def test_ib_withdrawal_is_excluded_from_the_deposit_leg(monkeypatch):
    """Bias #2 (case 110386): an IB-cum-trader whose commission cash-outs dwarf
    his trading money. The legacy net_deposit_hist would be
    5000 + (-1_220_000) = -1_215_000, making 淨賺 read as a ~$1.2M *gain*.
    The trading leg alone keeps him at a realistic (negative) 淨賺."""
    _patch(
        monkeypatch,
        userids={"1-1": 110386},
        nd_split={
            110386: {"trading_net_deposit": 5000.0, "ib_withdrawal": -1_220_000.0}
        },
        equity={110386: 1000.0},
    )
    out = ae.get_client_net_gain_map(object(), [{"server": "MT4_Live", "login": 1}])
    row = out["1-1"]
    assert row["client_trading_net_deposit"] == 5000.0  # ib_withdrawal NOT folded in
    # 淨賺 = 1000 - 5000 = -4000: he is net DOWN as a trader, which is the
    # truth. The legacy formula would have shown ≈ +$1.22M.
    assert row["client_equity"] - row["client_trading_net_deposit"] == -4000.0


def test_half_known_client_is_omitted_not_zero_filled(monkeypatch):
    """A client with equity but no net-deposit split (or vice versa) must drop
    out of the map so the column renders "—". A half-known difference is worse
    than no answer: it reads as a real number."""
    _patch(
        monkeypatch,
        userids={"1-1": 500, "1-2": 600},
        nd_split={500: {"trading_net_deposit": 5000.0, "ib_withdrawal": 0.0}},
        equity={500: 20000.0, 600: 9999.0},  # 600 has equity but no split
    )
    out = ae.get_client_net_gain_map(object(), _ALERTS)
    assert "1-1" in out
    assert "1-2" not in out


def test_unresolvable_clients_fail_open_to_empty(monkeypatch):
    """No userId resolves → empty map → callers write NULL → "—". Enrichment
    failure must never block alert persistence."""
    _patch(monkeypatch, userids={}, nd_split={}, equity={})
    assert ae.get_client_net_gain_map(object(), _ALERTS) == {}


def test_no_alerts_short_circuits(monkeypatch):
    _patch(monkeypatch, userids={}, nd_split={}, equity={})
    assert ae.get_client_net_gain_map(object(), []) == {}


def test_apply_client_net_gain_writes_keys_even_when_unknown():
    """Keys must always exist (explicit None) — the DB writer reads them
    positionally via alert.get(), and a silently absent key would be a
    same-shaped NULL today but a landmine if the writer ever grows a default."""
    alert = {}
    ae.apply_client_net_gain(alert, "1-9", {})
    assert alert == {"client_equity": None, "client_trading_net_deposit": None}


def test_apply_client_net_gain_handles_missing_loginsid():
    alert = {}
    ae.apply_client_net_gain(alert, None, {"1-1": {"client_equity": 1.0,
                                                  "client_trading_net_deposit": 2.0}})
    assert alert == {"client_equity": None, "client_trading_net_deposit": None}


def test_apply_client_net_gain_copies_both_operands():
    alert = {}
    gain = {"1-1": {"client_equity": 20000.0, "client_trading_net_deposit": 5000.0}}
    ae.apply_client_net_gain(alert, "1-1", gain)
    assert alert == {"client_equity": 20000.0, "client_trading_net_deposit": 5000.0}
