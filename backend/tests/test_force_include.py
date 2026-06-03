"""Tests for the force-include allowlist + demo_test_filter_sql helper.

`RISK_MONITOR_FORCE_INCLUDE_LOGINSIDS` lets the risk team validate rules with a
real account whose NAME/GROUP contains "test" (e.g. "test-acc") WITHOUT renaming
it: the account bypasses the demo/test filter and flows through detection like a
normal client. Applies to every non-Gap-Trade rule. These tests pin: parse,
the per-server SQL escape hatch, that an empty allowlist reproduces the original
filter byte-for-byte (zero behavior change), server isolation, and %%/%s safety.
"""

from __future__ import annotations

import app.core.sql_helpers as h


# --- parse + membership ---------------------------------------------------

def test_parse_basic(monkeypatch):
    monkeypatch.setenv("RISK_MONITOR_FORCE_INCLUDE_LOGINSIDS", "5-60000017, 1-8522845")
    assert h._parse_loginsid_env("RISK_MONITOR_FORCE_INCLUDE_LOGINSIDS") == {
        "5-60000017", "1-8522845"
    }


def test_parse_drops_malformed(monkeypatch):
    monkeypatch.setenv("RISK_MONITOR_FORCE_INCLUDE_LOGINSIDS", "5-60000017,foo,7,x-9")
    assert h._parse_loginsid_env("RISK_MONITOR_FORCE_INCLUDE_LOGINSIDS") == {"5-60000017"}


def test_is_force_included(monkeypatch):
    monkeypatch.setattr(h, "FORCE_INCLUDE_LOGINSIDS", {"5-60000017"})
    assert h.is_force_included("5-60000017") is True
    assert h.is_force_included("5-99999999") is False
    assert h.is_force_included("") is False
    assert h.is_force_included(None) is False


# --- demo_test_filter_sql: empty allowlist == original filter -------------

def test_filter_sql_empty_matches_original_mt5(monkeypatch):
    monkeypatch.setattr(h, "FORCE_INCLUDE_LOGINSIDS", set())
    out = h.demo_test_filter_sql("u.`Group`", "u.Name", login_col="c.Login", server_label="MT5")
    assert out == (
        "AND u.`Group` NOT LIKE '%%demo%%'"
        "\n          AND u.`Group` NOT LIKE '%%test%%'"
        "\n          AND COALESCE(u.Name, '') NOT LIKE '%%demo%%'"
        "\n          AND COALESCE(u.Name, '') NOT LIKE '%%test%%'"
    )


def test_filter_sql_group_only_variant(monkeypatch):
    monkeypatch.setattr(h, "FORCE_INCLUDE_LOGINSIDS", set())
    out = h.demo_test_filter_sql("u.`GROUP`", login_col="t.LOGIN", server_label="MT4_Live")
    assert out == (
        "AND u.`GROUP` NOT LIKE '%%demo%%'"
        "\n          AND u.`GROUP` NOT LIKE '%%test%%'"
    )


# --- demo_test_filter_sql: force-included wraps in OR escape --------------

def test_filter_sql_wraps_when_force_included(monkeypatch):
    monkeypatch.setattr(h, "FORCE_INCLUDE_LOGINSIDS", {"5-60000017"})
    out = h.demo_test_filter_sql("u.`Group`", "u.Name", login_col="c.Login", server_label="MT5")
    assert out.startswith("AND (c.Login IN (60000017) OR (")
    assert out.rstrip().endswith("))")
    assert "u.`Group` NOT LIKE '%%test%%'" in out


def test_filter_sql_server_isolation(monkeypatch):
    # A force-included MT5 login must NOT punch a hole in an MT4 query.
    monkeypatch.setattr(h, "FORCE_INCLUDE_LOGINSIDS", {"5-60000017"})
    mt4 = h.demo_test_filter_sql("u.`GROUP`", "u.NAME", login_col="t.LOGIN", server_label="MT4_Live")
    assert "IN (" not in mt4  # plain block, no escape hatch
    mt5 = h.demo_test_filter_sql("u.`Group`", "u.Name", login_col="c.Login", server_label="MT5")
    assert "c.Login IN (60000017)" in mt5


def test_filter_sql_multiple_same_server_sorted(monkeypatch):
    monkeypatch.setattr(h, "FORCE_INCLUDE_LOGINSIDS", {"5-300", "5-100"})
    out = h.demo_test_filter_sql("u.`Group`", login_col="c.Login", server_label="MT5")
    assert "c.Login IN (100, 300)" in out


def test_filter_sql_percent_and_param_safe(monkeypatch):
    # %% must survive (becomes literal % after PyMySQL formatting); no stray %s.
    monkeypatch.setattr(h, "FORCE_INCLUDE_LOGINSIDS", {"5-60000017"})
    out = h.demo_test_filter_sql("u.`Group`", "u.Name", login_col="c.Login", server_label="MT5")
    assert "%%demo%%" in out and "%%test%%" in out
    assert "%s" not in out
    # the fragment alone must %-format cleanly to a single-% literal
    assert (out % ()).count("%demo%") == 2
