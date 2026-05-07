"""
Tests for ``refresh_floating_for_alerts`` — the read-only helper behind
``GET /quick-profit/floating-refresh``.

We monkeypatch ``_get_connection`` and the SQL collectors so the test runs
purely in-process; the assertion focus is the merge / classify logic, not the
SQL itself.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple
from unittest.mock import MagicMock

import pytest

from app.services import rule_quick_profit_service as svc


@pytest.fixture(autouse=True)
def _stub_connection(monkeypatch):
    """Replace ``_get_connection`` with a no-op so tests don't hit MySQL."""
    fake_conn = MagicMock()
    fake_conn.close = MagicMock()
    monkeypatch.setattr(svc, "_get_connection", lambda _settings: fake_conn)
    return fake_conn


def _patch_floating(monkeypatch, mt4_map: Dict[Tuple[str, int], float],
                    mt5_map: Dict[Tuple[str, int], float]):
    def fake_mt4(conn, *, db_name, server_label, logins):
        return {k: v for k, v in mt4_map.items() if k[0] == server_label}

    def fake_mt5(conn, *, logins):
        return mt5_map

    monkeypatch.setattr(svc, "_query_mt4_floating", fake_mt4)
    monkeypatch.setattr(svc, "_query_mt5_floating", fake_mt5)


def _alert(id_: int, *, server: str = "MT4_Live", login: int = 100001,
           realized: float = 1000.0, floating: float = 4000.0,
           status: str = "mixed", currency: str = "USD") -> Dict[str, Any]:
    return {
        "id": id_,
        "server": server,
        "login": login,
        "symbol": "XAUUSD",
        "rule_id": svc.QUICK_PROFIT_RULE_ID_BASE,
        "realized_profit": realized,
        "floating_profit_snapshot": floating,
        "total_profit_usd": realized + floating,
        "position_status": status,
        "currency": currency,
    }


def test_empty_input_returns_empty_list(monkeypatch):
    _patch_floating(monkeypatch, {}, {})
    assert svc.refresh_floating_for_alerts(settings=None, alerts=[]) == []


def test_closed_alerts_short_circuit(monkeypatch):
    """``closed`` rows should pass through untouched (no SQL)."""
    _patch_floating(monkeypatch, {}, {})
    closed = _alert(1, status="closed", floating=0.0)
    out = svc.refresh_floating_for_alerts(settings=None, alerts=[closed])
    assert len(out) == 1
    assert out[0]["id"] == 1
    assert out[0]["floating_profit_snapshot"] == 0.0
    assert out[0]["position_status"] == "closed"


def test_open_alerts_pull_live_floating(monkeypatch):
    _patch_floating(
        monkeypatch,
        mt4_map={("MT4_Live", 200001): 7500.0},
        mt5_map={},
    )
    open_alert = _alert(2, server="MT4_Live", login=200001,
                        realized=2000.0, floating=4000.0, status="open")
    out = svc.refresh_floating_for_alerts(settings=None, alerts=[open_alert])
    assert len(out) == 1
    item = out[0]
    assert item["id"] == 2
    assert item["floating_profit_snapshot"] == 7500.0
    assert item["realized_profit"] == 2000.0
    # status flips to mixed because realized != 0 and floating != 0.
    assert item["position_status"] == "mixed"
    assert item["total_profit_usd"] == 9500.0


def test_cen_currency_normalises_floating(monkeypatch):
    """CEN accounts: live floating SQL returns cents → divide by 100."""
    _patch_floating(
        monkeypatch,
        mt4_map={("MT4_Live", 300001): 600000.0},  # 6000 USD in cents
        mt5_map={},
    )
    open_alert = _alert(3, server="MT4_Live", login=300001,
                        realized=1000.0, floating=500.0,
                        status="open", currency="CEN")
    out = svc.refresh_floating_for_alerts(settings=None, alerts=[open_alert])
    assert len(out) == 1
    assert out[0]["floating_profit_snapshot"] == 6000.0
    assert out[0]["total_profit_usd"] == 7000.0


def test_floating_collapsing_to_zero_flips_status_to_closed(monkeypatch):
    """If the user closed all positions since the alert fired, status
    should now report ``closed`` even if the persisted row was ``open``."""
    _patch_floating(monkeypatch, mt4_map={}, mt5_map={})
    open_alert = _alert(4, status="open", realized=2000.0, floating=4000.0)
    out = svc.refresh_floating_for_alerts(settings=None, alerts=[open_alert])
    assert out[0]["floating_profit_snapshot"] == 0.0
    assert out[0]["position_status"] == "closed"


def test_mixed_servers_routed_to_correct_query(monkeypatch):
    _patch_floating(
        monkeypatch,
        mt4_map={
            ("MT4_Live", 100001): 1234.0,
            ("MT4_Live2", 100002): 2345.0,
        },
        mt5_map={("MT5", 100003): 3456.0},
    )
    alerts = [
        _alert(10, server="MT4_Live", login=100001),
        _alert(11, server="MT4_Live2", login=100002),
        _alert(12, server="MT5", login=100003),
    ]
    out = svc.refresh_floating_for_alerts(settings=None, alerts=alerts)
    by_id = {it["id"]: it for it in out}
    assert by_id[10]["floating_profit_snapshot"] == 1234.0
    assert by_id[11]["floating_profit_snapshot"] == 2345.0
    assert by_id[12]["floating_profit_snapshot"] == 3456.0
