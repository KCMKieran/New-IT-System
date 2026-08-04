"""
Login IP export — one CSV row per correlated account.

The search grid collapses correlated accounts into a single cell; the CSV must
not. These tests pin the fan-out contract of `_explode_correlated` so a future
refactor can't quietly re-collapse the column.
"""

from __future__ import annotations

from app.services.login_ip_export_service import (
    _CSV_HEADERS_ACCOUNT,
    _CSV_HEADERS_IP,
    _explode_correlated,
)


def _account_row() -> dict:
    return {
        "kind": "account_id",
        "search_term": "8522845",
        "search_term_first_name": "Wei",
        "search_term_last_name": "Zhang",
        "client_id": "67036012",
        "date": "20260803",
        "server": "MT4",
        "login_ip": "1.2.3.4",
        "login_count": 12,
        "correlated_accounts": [
            {"login": "8530112", "first_name": "Ming", "last_name": "Li"},
            {"login": "8541003", "first_name": "Fang", "last_name": "Wang"},
            {"login": "8550228", "first_name": None, "last_name": None},
        ],
    }


def test_account_row_fans_out_one_row_per_peer():
    out = _explode_correlated(_account_row())

    assert len(out) == 3
    assert [r["correlated_login"] for r in out] == ["8530112", "8541003", "8550228"]
    assert [r["correlated_index"] for r in out] == [1, 2, 3]
    assert all(r["correlated_total"] == 3 for r in out)


def test_base_columns_repeat_on_every_exploded_row():
    out = _explode_correlated(_account_row())

    for r in out:
        assert r["search_term"] == "8522845"
        assert r["date"] == "20260803"
        assert r["server"] == "MT4"
        assert r["login_ip"] == "1.2.3.4"
        assert r["login_count"] == 12
        assert r["client_id"] == "67036012"


def test_peer_names_are_split_into_their_own_columns():
    out = _explode_correlated(_account_row())

    assert out[0]["correlated_last_name"] == "Li"
    assert out[0]["correlated_first_name"] == "Ming"
    # Missing CRM names become empty cells, never the string "None".
    assert out[2]["correlated_last_name"] == ""
    assert out[2]["correlated_first_name"] == ""


def test_ip_mode_peers_are_bare_account_ids():
    row = {
        "kind": "ip_address",
        "search_term": "1.2.3.4",
        "date": "20260803",
        "server": "MT5",
        "correlated_accounts": ["8530112", "8541003"],
    }

    out = _explode_correlated(row)

    assert len(out) == 2
    assert [r["correlated_login"] for r in out] == ["8530112", "8541003"]
    assert all(r["search_term"] == "1.2.3.4" for r in out)


def test_row_without_peers_survives_with_blank_correlated_cells():
    # Only reachable in ip_address mode — account_id rows with no peers are
    # dropped upstream in perform_search(). Dropping it here too would lose the
    # "this IP was seen that day" fact.
    row = {
        "kind": "ip_address",
        "search_term": "9.9.9.9",
        "date": "20260803",
        "server": "MT4",
        "correlated_accounts": [],
    }

    out = _explode_correlated(row)

    assert len(out) == 1
    assert out[0]["correlated_index"] == ""
    assert out[0]["correlated_total"] == 0
    assert out[0]["search_term"] == "9.9.9.9"


def test_exploded_keys_are_covered_by_the_csv_headers():
    # DictWriter uses extrasaction="ignore", so a key the headers don't list is
    # silently dropped from the file — assert the two stay in sync.
    for row, headers in (
        (_account_row(), _CSV_HEADERS_ACCOUNT),
        (
            {
                "kind": "ip_address",
                "search_term": "1.2.3.4",
                "date": "20260803",
                "server": "MT5",
                "correlated_accounts": ["8530112"],
            },
            _CSV_HEADERS_IP,
        ),
    ):
        for out_row in _explode_correlated(row):
            missing = set(out_row) - set(headers) - {"kind"}
            assert not missing, f"columns dropped from CSV: {missing}"
