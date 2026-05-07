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

# Add ``backend/`` (parent of this conftest) so ``import app.xxx`` resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
