from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import pymysql

from ..core.config import Settings
from ..schemas.ib_tree import IBTreeNode, IBTreeResponse

logger = logging.getLogger(__name__)

# tags.categoryId = 1 ("CN and Global_Staff Code") marks internal sales/staff
# accounts. Same convention as the sales-belong-autofill project.
STAFF_TAG_CATEGORY_ID = 1

# Sales code shape: letters + digits, optional dot-suffix (e.g. HZL013.M).
_CODE_RE = re.compile(r"^[A-Za-z]+\d+(?:\.[A-Za-z0-9]+)?")

CLIENT_QUERY = """
SELECT
    u.id,
    u.firstName,
    u.lastName,
    JSON_UNQUOTE(cn.v) AS chinese_name,
    JSON_UNQUOTE(sb.v) AS sales_belong,
    EXISTS(
        SELECT 1 FROM fxbackoffice.user_tags ut
        JOIN fxbackoffice.tags tg ON tg.id = ut.tagId AND tg.categoryId = %s
        WHERE ut.userId = u.id
    ) AS is_staff
FROM fxbackoffice.users u
LEFT JOIN fxbackoffice.user_custom_fields cn
       ON cn.userId = u.id AND cn.k = 'custom_chinese_name'
LEFT JOIN fxbackoffice.user_custom_fields sb
       ON sb.userId = u.id AND sb.k = 'custom_sales_belong'
WHERE u.id = %s
"""

# Ancestor chain via the closure table, topmost ancestor first.
# For non-IB clients level 0 is the direct IB (no self row), for IB clients
# the self row is filtered out in Python by id.
ANCESTORS_QUERY = """
SELECT
    t.level,
    u.id,
    u.firstName,
    u.lastName,
    JSON_UNQUOTE(cn.v) AS chinese_name,
    EXISTS(
        SELECT 1 FROM fxbackoffice.user_tags ut
        JOIN fxbackoffice.tags tg ON tg.id = ut.tagId AND tg.categoryId = %s
        WHERE ut.userId = u.id
    ) AS is_staff
FROM fxbackoffice.ib_tree_with_self t
JOIN fxbackoffice.users u ON u.id = t.ibId
LEFT JOIN fxbackoffice.user_custom_fields cn
       ON cn.userId = u.id AND cn.k = 'custom_chinese_name'
WHERE t.referralId = %s
ORDER BY t.level DESC, u.id
"""


def _connect(settings: Settings):
    """Create a MySQL connection using shared FX backoffice credentials."""
    if not settings.DB_HOST:
        raise RuntimeError("DB_HOST is not configured")

    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


def _display_name(row: Dict[str, Any]) -> str:
    """Chinese name when available, otherwise pinyin firstName lastName."""
    chinese = (row.get("chinese_name") or "").strip()
    if chinese:
        return chinese
    return f"{(row.get('firstName') or '').strip()} {(row.get('lastName') or '').strip()}".strip()


def _sales_code(last_name: Optional[str]) -> str:
    """Extract the code-shaped prefix of a staff lastName (HZL013.M → HZL013.M)."""
    name = (last_name or "").strip()
    m = _CODE_RE.match(name)
    return m.group(0) if m else name


def _node(row: Dict[str, Any], role: str) -> IBTreeNode:
    return IBTreeNode(
        user_id=int(row["id"]),
        display_name=_display_name(row),
        english_name=f"{(row.get('firstName') or '').strip()} {(row.get('lastName') or '').strip()}".strip(),
        role=role,
        is_staff=bool(row.get("is_staff")),
    )


def query_ib_tree(settings: Settings, client_id: int) -> Optional[IBTreeResponse]:
    """Build the IB hierarchy chain for one client.

    Display rule: the run of staff accounts at the top of the chain collapses
    into a single sales head shown as "id code"; real-person IBs follow as
    "id name"; the client closes the chain. Staff accounts anywhere in the
    chain show their sales code instead of a personal name — the chain text
    is pasted to external venues.
    """
    with _connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(CLIENT_QUERY, (STAFF_TAG_CATEGORY_ID, client_id))
            client_row = cur.fetchone()
            if not client_row:
                return None
            cur.execute(ANCESTORS_QUERY, (STAFF_TAG_CATEGORY_ID, client_id))
            ancestor_rows: List[Dict[str, Any]] = list(cur.fetchall())

    # ib_tree_with_self contains a self row for IB clients — drop it.
    ancestors = [r for r in ancestor_rows if int(r["id"]) != client_id]

    # Collapse the leading staff run (topmost internal accounts) into one head.
    idx = 0
    staff_run: List[Dict[str, Any]] = []
    while idx < len(ancestors) and ancestors[idx].get("is_staff"):
        staff_run.append(ancestors[idx])
        idx += 1

    # Head label cascade keeps the head from silently vanishing on dirty
    # data: staff lastName code → client's own sales_belong → staff name.
    client_sales_belong = (client_row.get("sales_belong") or "").strip() or None
    if staff_run:
        head_staff = staff_run[-1]
        sales_code = (
            _sales_code(head_staff.get("lastName"))
            or client_sales_belong
            or _display_name(head_staff)
        )
        head_id: Optional[int] = int(head_staff["id"])
    else:
        sales_code = client_sales_belong
        head_id = None

    ib_rows = ancestors[idx:]

    def _chain_part(row: Dict[str, Any]) -> str:
        # Staff accounts show their sales code, never a personal name.
        label = (
            (_sales_code(row.get("lastName")) or _display_name(row))
            if row.get("is_staff")
            else _display_name(row)
        )
        return f"{int(row['id'])} {label}"

    nodes: List[IBTreeNode] = []
    if sales_code:
        nodes.append(IBTreeNode(
            user_id=head_id,
            display_name=sales_code,
            english_name=sales_code,
            role="sales",
            is_staff=True,
        ))
    nodes.extend(_node(r, "ib") for r in ib_rows)
    nodes.append(_node(client_row, "client"))

    parts: List[str] = []
    if sales_code:
        parts.append(f"{head_id} {sales_code}" if head_id is not None else sales_code)
    parts.extend(_chain_part(r) for r in ib_rows)
    client_part = f"{client_id} {_display_name(client_row)}"
    if len(parts) == 0:
        client_part += "（无上级代理）"
    parts.append(client_part)

    return IBTreeResponse(
        client_id=client_id,
        sales_code=sales_code,
        chain_text=" > ".join(parts),
        nodes=nodes,
    )
