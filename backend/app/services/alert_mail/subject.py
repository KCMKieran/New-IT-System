"""Shared subject-line builder for every Alert Mail Center source.

A Chinese prefix is the point: this mailbox already receives several
unrelated automated reports ([GAP-TAG], CS Report - IB Financial, MT服务器
关联账户登录警报, IB Financial Monitor), and an English "[Risk Alert]" did
not stand out among them. Risk digests carry a Chinese tag so they can be
spotted — and filtered — at a glance.

Sources may override that tag with a per-source `prefix` to stay
distinguishable from each other, not just from the non-risk reports above
(hedge_open uses "[对冲刷单]"). The override belongs to the *source*, not the
rule: a band holds up to 10 rules and they share one prefix, so the prefix
count tracks the handful of registered sources rather than the rule list —
mailbox filters do not need updating every time a rule is added.

When a source overrides the prefix, its `label` carries the English name
alone ("Hedge Open") — the Chinese identifier already lives in the prefix,
and repeating it reads as stutter. Sources on the default prefix keep the
bilingual label ("返佣套利 Rebate Arbitrage"): the Chinese half is what the
reader scans for, the English half is what search queries still hit.

This module deliberately imports nothing from the package: registry imports
alert_mail_dispatcher at top level while the dispatcher imports registry
lazily inside functions, so a shared helper living in either one would
reintroduce that cycle for the other importer.
"""

from __future__ import annotations

SUBJECT_PREFIX = "[风控告警]"
TEST_PREFIX = "[TEST]"


def build_subject(
    label: str,
    tail: str,
    test: bool = False,
    prefix: str = SUBJECT_PREFIX,
) -> str:
    """Compose one digest subject: "{prefix} {label} — {tail}".

    `label` is the module label ("Hedge Open"), `tail` the per-source hit
    summary ("3 accounts suspected"). `prefix` defaults to the shared
    "[风控告警]" tag; a source passes its own to stay distinguishable from
    the other risk digests. Test-sends get a leading "[TEST] " so they never
    read as a live alert.

    The space after [TEST] is deliberate — "[TEST][对冲刷单] …" reads as one
    run-on token at a glance, which is the opposite of what the marker is for.
    """
    subject = f"{prefix} {label} — {tail}"
    return f"{TEST_PREFIX} {subject}" if test else subject
