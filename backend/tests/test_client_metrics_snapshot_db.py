"""OPT-0060 SQLite layer guardrails (2026-09-03 cold-review findings 1 & 2).

Finding 2: carry_over_mdd_from_live must preserve MDD-ONLY rows (fully-wiped
clients have no ROACE row — an UPDATE-only carry-over silently drops exactly
the wipeout=1 blow-ups the feature exists to show).

Finding 1: the refresh mutex must work ACROSS processes (threading.Lock covers
one worker; manual refreshes land on any of the 4 prod workers and the dev
container shares the same bind-mounted file), so it lives in the SQLite file.
"""

from __future__ import annotations

import pytest

from app.core import client_roace_db as db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "client_roace_test.db")
    db.init_client_roace_db()
    return db


def _seed_live(tmp_db, rows):
    with tmp_db._get_conn() as conn:
        for r in rows:
            cols = ", ".join(r)
            ph = ", ".join(["?"] * len(r))
            conn.execute(
                f"INSERT INTO client_metrics_snapshot ({cols}) VALUES ({ph})",
                list(r.values()),
            )


class TestCarryOver:
    def test_mdd_only_rows_survive_a_carry_over(self, tmp_db):
        """The wiped client (MDD row, no ROACE row) must still be in staging
        after carry-over — losing it means every blow-up vanishes from the
        page on any MDD-leg failure."""
        _seed_live(tmp_db, [
            {  # normal client: ROACE + MDD
                "user_id": 1, "avg_daily_equity": 1000.0, "active_days": 100,
                "refreshed_at": "2026-09-02 06:00:00",
                "mdd_all": 12.5, "mdd_status_all": "ok",
                "mdd_refreshed_at": "2026-09-02 06:00:00",
            },
            {  # fully-wiped client: MDD only, no ROACE columns
                "user_id": 2, "mdd_all": 100.0, "mdd_status_all": "ok",
                "wipeout": 1, "mdd_refreshed_at": "2026-09-02 06:00:00",
            },
        ])
        tmp_db.begin_metrics_staging()
        # fresh ROACE leg wrote client 1 only (client 2 has no ROACE row)
        tmp_db.upsert_roace_batch(
            [(1, 1100.0, None, None, None, None, 101)], "2026-09-03 06:00:00"
        )
        carried = tmp_db.carry_over_mdd_from_live()
        assert carried == 2  # 1 updated + 1 inserted
        tmp_db.commit_metrics_staging()

        snap = tmp_db.bulk_get_roace([1, 2])
        # client 1: fresh ROACE + previous-generation MDD (old stamp visible)
        assert snap[1]["avg_daily_equity"] == 1100.0
        assert snap[1]["mdd_all"] == 12.5
        assert snap[1]["mdd_refreshed_at"] == "2026-09-02 06:00:00"
        # client 2: the wiped client survived, flags intact
        assert 2 in snap, "MDD-only (wiped) client dropped by carry-over"
        assert snap[2]["mdd_all"] == 100.0
        assert snap[2]["wipeout"] == 1

    def test_failed_run_keeps_previous_generation(self, tmp_db):
        """H1: begin + abort must leave the live table untouched."""
        _seed_live(tmp_db, [{
            "user_id": 7, "avg_daily_equity": 500.0, "active_days": 50,
            "refreshed_at": "2026-09-02 06:00:00", "mdd_all": 40.0,
        }])
        tmp_db.begin_metrics_staging()
        tmp_db.upsert_roace_batch(
            [(7, 999.0, None, None, None, None, 51)], "2026-09-03 06:00:00"
        )
        tmp_db.abort_metrics_staging()
        snap = tmp_db.bulk_get_roace([7])
        assert snap[7]["avg_daily_equity"] == 500.0  # previous generation


class TestRefreshLock:
    def test_second_holder_is_refused_while_first_is_live(self, tmp_db):
        assert tmp_db.try_acquire_refresh_lock("worker-a") is True
        assert tmp_db.try_acquire_refresh_lock("worker-b") is False

    def test_release_frees_the_lock(self, tmp_db):
        assert tmp_db.try_acquire_refresh_lock("worker-a") is True
        tmp_db.release_refresh_lock("worker-a")
        assert tmp_db.try_acquire_refresh_lock("worker-b") is True

    def test_release_of_a_non_holder_is_a_noop(self, tmp_db):
        assert tmp_db.try_acquire_refresh_lock("worker-a") is True
        tmp_db.release_refresh_lock("worker-b")  # not the holder
        assert tmp_db.try_acquire_refresh_lock("worker-c") is False

    def test_stale_claim_is_taken_over(self, tmp_db, monkeypatch):
        assert tmp_db.try_acquire_refresh_lock("dead-worker") is True
        # age the claim past the staleness horizon
        with tmp_db._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM roace_meta WHERE key = 'refresh_lock'"
            ).fetchone()
            holder, ts = row["value"].rsplit("|", 1)
            aged = f"{holder}|{float(ts) - db._REFRESH_LOCK_STALE_S - 10}"
            conn.execute(
                "UPDATE roace_meta SET value = ? WHERE key = 'refresh_lock'",
                (aged,),
            )
        assert tmp_db.try_acquire_refresh_lock("worker-b") is True
