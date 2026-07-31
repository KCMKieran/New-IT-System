"""Daily correlation alert: subject line and email-body rendering.

The layout rules pinned here come from the `alert-email-style` skill and are
not cosmetic preferences: mail clients do not reflow wide tables, they shrink
the entire message to fit the widest element, so a single wide grid or one
`nowrap` value cell makes the whole alert unreadable on a phone.
"""

import re

import pytest

from app.services import login_ip_report_service as R


def _account(**over):
    """A monitored-account entry shaped like `build_report_data` emits."""
    base = {
        "remarks": None,
        "server_name": "MT4",
        "logged_in": True,
        "total_logins": 4,
        "used_ips": {"1.1.1.1"},
        "logins_by_ip": {"1.1.1.1": 4},
        "shared_ips_analysis": {},
    }
    base.update(over)
    return base


def _hit(ip="1.1.1.1", corr_id="999", server="MT5", hist="20260730"):
    return {ip: {"logins_on_this_ip": 1, "correlated_accounts": [
        {"id": corr_id, "historical_date": hist, "server_name": server},
    ]}}


# ── Subject ────────────────────────────────────────────────────────────────


def test_subject_uses_the_required_chinese_prefix_and_counts():
    rd = {"8613863": _account(shared_ips_analysis=_hit()), "111": _account()}
    subject = R.build_subject("20260730", rd)
    assert subject.startswith("[重要客户IP监控] 2026-07-30")
    assert "命中 1 个监控账户" in subject
    assert "5" not in subject  # only real counts, no stray numbers
    assert "1 个关联账户" in subject


def test_subject_counts_each_correlated_account_once_across_ips():
    """Same person on two shared IPs is one correlated account, not two."""
    shared = {
        "1.1.1.1": {"logins_on_this_ip": 1, "correlated_accounts": [
            {"id": "999", "historical_date": "20260730", "server_name": "MT4"}]},
        "2.2.2.2": {"logins_on_this_ip": 1, "correlated_accounts": [
            {"id": "999", "historical_date": "20260730", "server_name": "MT4"}]},
    }
    subject = R.build_subject("20260730", {"8613863": _account(shared_ips_analysis=shared)})
    assert "1 个关联账户" in subject


# ── Body: mobile / Outlook layout invariants ───────────────────────────────


@pytest.fixture()
def body():
    rd = {
        "8613863": _account(remarks="Law 重点客户观察", shared_ips_analysis=_hit()),
        "8521406": _account(logged_in=False, total_logins=0, used_ips=set()),
    }
    return R.render_html_report(
        "20260730", rd,
        {"999": {"client_id": 123456, "chinese_name": "杜令波"}},
        {"1.1.1.1": "CN"},
        "raw.csv",
    )


def test_body_is_width_bounded_for_phones_and_outlook(body):
    assert "max-width:600px" in body
    assert 'name="viewport"' in body
    # Outlook desktop ignores max-width; the mso conditional pins it there.
    assert "<!--[if mso]>" in body


def _cells_per_row(html_text: str) -> list[int]:
    """Count each `<tr>`'s DIRECT `<td>` children.

    A regex can't do this: the layout nests tables, so a naive `<tr>.*?</tr>`
    slice counts the inner table's cells against the outer row.
    """
    from html.parser import HTMLParser

    class _Counter(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack: list[str] = []
            self.counts: list[int] = []
            self._per_row: list[int] = []

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._per_row.append(0)
            elif tag == "td" and self._per_row:
                # Attribute the cell to the nearest enclosing <tr>.
                self._per_row[-1] += 1
            if tag not in ("br", "meta", "img"):
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag == "tr" and self._per_row:
                self.counts.append(self._per_row.pop())
            if tag in self.stack:
                self.stack.pop()

    p = _Counter()
    p.feed(html_text)
    return p.counts


def test_body_never_uses_a_wide_multi_column_grid(body):
    """Only the 2-column label:value shape is allowed."""
    assert max(_cells_per_row(body)) <= 2


def test_value_cells_never_carry_nowrap(body):
    """nowrap on a value is the #1 cause of shrink-to-fit on mobile."""
    assert "nowrap" not in body


def test_body_has_no_emoji(body):
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", body)


def test_body_stays_small_enough_to_avoid_gmail_clipping(body):
    # Gmail silently truncates past ~102KB; raw logs go to CSV to keep it far
    # below that no matter how many correlations land.
    assert len(body.encode("utf-8")) < 20_000


# ── Body: content ──────────────────────────────────────────────────────────


def test_correlated_account_shows_its_own_server_not_the_monitored_one(body):
    """An MT5 account correlating to a monitored MT4 account is normal and
    must be labelled, or the reader sees an MT5 id under an MT4 heading."""
    assert "MT5" in body


def test_ip_rendered_with_country_when_geo_resolved(body):
    assert "1.1.1.1 (CN)" in body


def test_ip_falls_back_to_bare_when_geo_unavailable():
    rd = {"8613863": _account(shared_ips_analysis=_hit())}
    out = R.render_html_report("20260730", rd, {}, {}, None)
    assert "1.1.1.1" in out
    assert "(None)" not in out


def test_geo_lookup_failure_degrades_instead_of_killing_the_alert(monkeypatch):
    """A MaxMind outage must never suppress a correlation alert."""
    import app.services.login_ip_geo_service as geo

    monkeypatch.setattr(
        geo, "resolve_countries",
        lambda ips: (_ for _ in ()).throw(RuntimeError("maxmind down")),
    )
    assert R._resolve_ip_countries(["1.1.1.1"]) == {}


def test_untrusted_text_is_escaped(body):
    rd = {"8613863": _account(
        remarks="<script>alert(1)</script>", shared_ips_analysis=_hit())}
    out = R.render_html_report("20260730", rd, {}, {}, None)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_accounts_without_hits_are_summarised_not_given_full_blocks(body):
    """The legacy layout rendered an empty card per idle account (10 of 11 on
    a typical day), which buried the one that mattered."""
    assert "NO CORRELATION · 未发现关联" in body
    assert body.count("Logins today · 当日登录") == 1


# ── CSV attachment ─────────────────────────────────────────────────────────


def test_raw_logins_csv_covers_monitored_and_correlated_accounts(tmp_path):
    rd = {"8613863": _account(shared_ips_analysis=_hit(corr_id="999"))}
    raw = {"MT4": {"8613863": ["2\t13:32:53\t1.1.1.1\t'8613863': login (x)"]},
           "MT5": {"999": ["2\t13:33:01\t1.1.1.1\t'999': login (y)"]}}
    path = R._build_raw_logins_csv("20260730", rd, raw)
    try:
        text = path.read_text(encoding="utf-8-sig")
        assert "account_id,role,raw_log_line" in text
        assert "8613863,monitored" in text
        assert "999,correlated" in text
    finally:
        R._cleanup_temp_csv(path)
    assert not path.exists()


def test_no_csv_when_there_are_no_raw_lines():
    rd = {"8613863": _account(shared_ips_analysis=_hit())}
    assert R._build_raw_logins_csv("20260730", rd, {}) is None
