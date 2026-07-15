"""Shared subject-line builder for every Alert Mail Center source.

The Chinese prefix is the point: this mailbox already receives several
unrelated automated reports ([GAP-TAG], CS Report - IB Financial, MT服务器
关联账户登录警报, IB Financial Monitor), and an English "[Risk Alert]" did
not stand out among them. Risk digests carry 风控告警 so they can be spotted
— and filtered — at a glance.

Keep the bilingual `label` ("对冲刷单 Hedge Open"): the Chinese half is what
the reader scans for, the English half is what search queries still hit.

This module deliberately imports nothing from the package: registry imports
alert_mail_dispatcher at top level while the dispatcher imports registry
lazily inside functions, so a shared helper living in either one would
reintroduce that cycle for the other importer.
"""

from __future__ import annotations

SUBJECT_PREFIX = "[风控告警]"
TEST_PREFIX = "[TEST]"


def build_subject(label: str, tail: str, test: bool = False) -> str:
    """Compose one digest subject: "[风控告警] {label} — {tail}".

    `label` is the bilingual module label ("对冲刷单 Hedge Open"), `tail` the
    per-source hit summary ("3 个账户涉嫌刷佣"). Test-sends get a leading
    [TEST] so they never read as a live alert.
    """
    subject = f"{SUBJECT_PREFIX} {label} — {tail}"
    return f"{TEST_PREFIX}{subject}" if test else subject
