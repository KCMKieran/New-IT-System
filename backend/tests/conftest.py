"""
Pytest configuration: add the ``backend`` folder to ``sys.path`` so test files
can import ``app.*`` modules just like the production entrypoint does.

Run from the repo root:
    cd backend && .venv/bin/pytest tests/

If pytest is missing in the venv:
    .venv/bin/pip install pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add ``backend/`` (parent of this conftest) so ``import app.xxx`` resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def pytest_configure(config):
    """Register the ``slow`` marker.

    Registered here rather than in a pytest.ini so the repo keeps its current
    rootdir resolution — there is no pytest config file today, and adding one
    would change how every existing invocation resolves paths.

    ``slow`` means: talks to a live cloud database AND costs minutes, not
    seconds. ``verify.sh`` deselects it by default and says so in its output;
    run ``./verify.sh --full`` (or ``pytest -m slow``) to include it.
    """
    config.addinivalue_line(
        "markers",
        "slow: live cloud-DB integration test measured in minutes; "
        "deselected by verify.sh unless --full is passed",
    )


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Drop the cached Settings around every test.

    ``get_settings()`` is ``@lru_cache``d for the per-request hot path (auth
    P3.5). Without this, the first test to build Settings would freeze that
    process's env for every later test, so any ``monkeypatch.setenv`` would be
    silently ignored — a failure mode that looks like the code ignoring config.
    Cleared on both sides so a test cannot leak its env into the next one.
    """
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_auth_event_throttle():
    """Drop the auth_events refusal throttle around every test.

    ``record_auth_event`` rate-limits refusal rows per (event, email, ip) in a
    PROCESS-LEVEL dict — deliberately, since the alternative is letting an
    unauthenticated caller append to users.db without bound. That dict is not
    reset by pointing ``users_db._DB_PATH`` at a tmp file, so refusals written
    by one test spend the next test's budget: add a few 403 assertions
    anywhere in the suite and some later test's "the refusal was recorded"
    assertion goes red, in a file that did not change.

    Hit for real on 2026-08-19 (the dashboard-module tests pushed
    test_admin_api's manager-refusal assertion over the per-minute limit), so
    the isolation lives here rather than in whichever file noticed it.
    """
    from app.services import auth_service

    with auth_service._throttle_lock:
        auth_service._throttle_counts.clear()
    yield
    with auth_service._throttle_lock:
        auth_service._throttle_counts.clear()
