from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import pymysql

from ..core.config import Settings
from ..core.data_scope import cid_for_crm_user_ids
from ..schemas.ib_tree import IBTreeNode, IBTreeResponse

logger = logging.getLogger(__name__)

# What an out-of-scope node's identity is replaced with. An em dash rather than
# "***" or "REDACTED" because this chain is pasted into tickets and to external
# venues — it has to read as "a link we are not naming", not as a system error.
MASKED_LABEL = "—"

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


def _mask_out_of_scope(
    settings: Settings,
    allowed_cids: frozenset[int],
    nodes: List[IBTreeNode],
    parts: List[str],
) -> Tuple[List[IBTreeNode], List[str], bool]:
    """Blank the IDENTITY of every chain node outside the caller's cids.

    Why mask rather than drop. The input gate (``require_cids_allowed`` in the
    route) already checked the CLIENT the caller named — but the response is
    that client's UPLINE, i.e. the other side of the IB relationship, and 11
    Global clients sit under a CN IB. Dropping those links would hand CS a
    chain with a hole in it: "sales > client" where a real IB sits in between
    reads as "this client has no agent", which is not a smaller truth, it is a
    different and wrong one. So the shape is preserved node-for-node and only
    the identity goes.

    What counts as identity, and why the list is longer than it looks:
    ``user_id`` (a CRM id is a name you can look up), ``display_name`` and
    ``english_name``. ``role`` / ``is_staff`` stay — they describe the SHAPE of
    the chain, not who anybody is. ``chain_text`` is rebuilt from the masked
    parts by the caller; it is the field most likely to be forgotten, because
    it is a SECOND, pre-rendered copy of every name in the chain and passing it
    through untouched would leak the whole thing while every node in ``nodes``
    looked correctly redacted.

    Fail closed on an unresolvable cid. ``cid_for_crm_user_ids`` maps "not in
    the CRM", "cid is NULL" and "a cid nobody told us about" all onto ``None``,
    and ``None not in allowed_cids``, so every one of them masks. That matters
    here specifically: ``ib_data_service._get_company_name`` renders an
    unrecognised cid as the visible string ``"Unknown(2)"``, and the equivalent
    mistake on this path would be an unrecognised entity's IB rendering as a
    real name to precisely the two people scoped away from it.

    A node with ``user_id is None`` is NOT masked, and that is the one
    deliberate exception. It happens only for the synthetic sales head built
    from the client's own ``custom_sales_belong`` custom field when no staff
    row was found upstream — there is no user row behind it to be in or out of
    scope, and the value is a column of the client the caller was just cleared
    to see. Masking it would redact the caller's own data and, worse, would set
    ``data_scope_filtered`` on virtually every query, which turns the frontend
    notice into noise nobody reads.

    Costs one batched resolver query, and only for a restricted caller: the
    caller short-circuits on ``allowed_cids is None`` before getting here.
    ``cid_for_crm_user_ids`` carries its own MAX_EXECUTION_TIME / read_timeout /
    autocommit guards, which is why the resolution is NOT reimplemented as an
    extra column on CLIENT_QUERY / ANCESTORS_QUERY: that would put a second
    copy of "how do I turn an id into a cid" in the codebase, and the direction
    those copies drift is the one that forgets the timeout (2026-08-09).
    """
    # nodes and parts are built in lockstep below (same optional head, same ib
    # rows, same trailing client), so index i of one describes index i of the
    # other. If that ever stops being true the zip below silently truncates the
    # chain, so keep them constructed together.
    ids = sorted({n.user_id for n in nodes if n.user_id is not None})
    resolved = cid_for_crm_user_ids(settings, ids) if ids else {}

    masked_nodes: List[IBTreeNode] = []
    masked_parts: List[str] = []
    any_masked = False
    for node, part in zip(nodes, parts):
        if node.user_id is None or resolved.get(node.user_id) in allowed_cids:
            masked_nodes.append(node)
            masked_parts.append(part)
            continue
        any_masked = True
        masked_nodes.append(
            node.model_copy(
                update={
                    "user_id": None,
                    "display_name": MASKED_LABEL,
                    "english_name": MASKED_LABEL,
                }
            )
        )
        masked_parts.append(MASKED_LABEL)

    return masked_nodes, masked_parts, any_masked


def query_ib_tree(
    settings: Settings,
    client_id: int,
    allowed_cids: Optional[frozenset[int]] = None,
) -> Optional[IBTreeResponse]:
    """Build the IB hierarchy chain for one client.

    Display rule: the run of staff accounts at the top of the chain collapses
    into a single sales head shown as "id code"; real-person IBs follow as
    "id name"; the client closes the chain. Staff accounts anywhere in the
    chain show their sales code instead of a personal name — the chain text
    is pasted to external venues.

    ``allowed_cids`` is the caller's country data scope, ``None`` meaning
    UNRESTRICTED — never an empty set, and never tested for truthiness (an
    empty set would mean "may see nothing", the opposite of "no restriction";
    same ``[]`` vs ``["*"]`` trap as allowed_modules). When it is ``None`` this
    function runs exactly as it did before the scope existed: no extra query,
    no extra predicate, and ``data_scope_filtered`` false.
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

    # Row-level (country) data scope, applied to the RELATED side. Everything
    # above answered "what is this client's chain"; this narrows it to "...that
    # you may see". The short-circuit is the 99% path — ~30 colleagues are
    # unrestricted and must not pay a resolver round-trip so that two are not.
    data_scope_filtered = False
    if allowed_cids is not None:
        nodes, parts, data_scope_filtered = _mask_out_of_scope(
            settings, allowed_cids, nodes, parts
        )
        # sales_code is a SECOND copy of the head node's label and is rendered
        # on its own in the UI. Masking the node while leaving this field
        # holding the same string would redact nothing at all.
        if nodes and nodes[0].role == "sales" and nodes[0].display_name == MASKED_LABEL:
            sales_code = MASKED_LABEL
        logger.info(
            "ib-tree data scope applied: client=%s allowed_cids=%s masked=%s",
            client_id, sorted(allowed_cids), data_scope_filtered,
        )

    return IBTreeResponse(
        client_id=client_id,
        sales_code=sales_code,
        chain_text=" > ".join(parts),
        nodes=nodes,
        data_scope_filtered=data_scope_filtered,
    )
