"""
Anti-drift: frontend SERVER_SORTABLE ⇄ backend SORTABLE_ACTIVITY_COLS.

The full-client-universe view's sortable-column whitelist is hand-mirrored
between the backend (risk_cases_service.SORTABLE_ACTIVITY_COLS, enforced as
a 422 by the /risk-cases/activity-clients route) and the frontend
(SERVER_SORTABLE in ActivityClientsPanel.tsx, which decides whether a header
click triggers a server-side sort). New columns land weekly; when the two
sets drift the UI either 422s on a click (frontend-only key) or shows a
dead/unsortable header (backend-only key). Same cross-language pattern as
the view-profiles manifest anti-drift test
(frontend/src/lib/view-profiles/view-profiles.test.ts): regex-parse the
other side's source and compare as sets, both directions.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.risk_cases_service import SORTABLE_ACTIVITY_COLS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_TSX = (
    _REPO_ROOT / "frontend" / "src" / "pages" / "ActivityClientsPanel.tsx"
)


def _frontend_server_sortable() -> frozenset[str]:
    """Parse the SERVER_SORTABLE Set literal out of ActivityClientsPanel.tsx."""
    source = _PANEL_TSX.read_text(encoding="utf-8")
    m = re.search(
        r"const\s+SERVER_SORTABLE\s*=\s*new\s+Set\s*\(\s*\[(.*?)\]\s*\)",
        source,
        re.DOTALL,
    )
    assert m, (
        f"could not find `const SERVER_SORTABLE = new Set([...])` in "
        f"{_PANEL_TSX} — if the constant was renamed/moved, update this "
        "test's regex alongside it"
    )
    keys = re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
    assert keys, f"SERVER_SORTABLE parsed empty from {_PANEL_TSX}"
    return frozenset(keys)


def test_server_sortable_matches_backend_whitelist():
    frontend = _frontend_server_sortable()
    backend = frozenset(SORTABLE_ACTIVITY_COLS)

    backend_only = sorted(backend - frontend)
    frontend_only = sorted(frontend - backend)

    assert not backend_only and not frontend_only, (
        "Sortable-column whitelists drifted between backend and frontend.\n"
        f"  Backend-only keys {backend_only}: add them to SERVER_SORTABLE in "
        "frontend/src/pages/ActivityClientsPanel.tsx (plus sortable:true + "
        "no-op comparator on the column) or drop them from the backend "
        "whitelist.\n"
        f"  Frontend-only keys {frontend_only}: register them in "
        "backend/app/services/risk_cases_service.py (_ACTIVITY_SORT_COL_SQL "
        "for driver-layer columns, _ACTIVITY_METRIC_SORT + its CTE for "
        "enrichment metrics) — until then the route 422s on that sort.\n"
        "  See docs/features/risk-watchlist.md 新增列 checklist."
    )
