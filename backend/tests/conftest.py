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
