"""
Login IP Monitor — manual search service.

Ports `46-MT-Server-Login-Detect/search.py` `perform_search()` to the new
platform. Backs the `POST /api/v1/login-ip/search` endpoint (Tab 3).

Two search modes
----------------
1. `account_id` — given account(s), find every IP they used per day and
   other accounts on those IPs. **Only if client_id (CRM `users.id`) differs**
   from the search account is a peer shown — same `userId` as the search
   login is skipped (one natural person, multiple MT logins is noise).
   Legacy rule from `search.py`.

2. `ip_address` — given IP(s), list the accounts seen on each IP per day.
   No same-client filter here (an IP can legitimately be used by multiple
   customers of the same entity).

The JSON files produced by `login_ip_analyzer_service` are the single
source of truth — no MT log file is touched here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_DATA_BASE_DIR = _BACKEND_ROOT / "data" / "login_ip"

_IP_MAPPING_FILE = "analysis_ip_to_accounts.json"
_ACCOUNT_LOGINS_FILE = "analysis_account_logins.json"


def _available_dates(base_dir: Path) -> list[str]:
    """Return YYYYMMDD subdirectory names, newest first. Skips `tmp/`."""
    if not base_dir.is_dir():
        return []
    valid: list[str] = []
    for sub in base_dir.iterdir():
        if not sub.is_dir() or sub.name == "tmp":
            continue
        if len(sub.name) == 8 and sub.name.isdigit():
            valid.append(sub.name)
    return sorted(valid, reverse=True)


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("search: failed to read %s: %s", path, exc)
        return None


def perform_search(
    search_type: str,
    terms: list[str],
    days: int,
    data_base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Run batch search and correlation analysis for the last `days` days.

    Returns one of three shapes (matches legacy `perform_search`):
    - `{"results": [...]}`  success with matches
    - `{"not_found": str}`  no matches in the window
    - `{"error": str}`      input or dependency error

    Args:
        search_type: 'account_id' or 'ip_address'
        terms: non-empty list of search strings. De-duped internally.
        days: 1–30. Number of recent days to scan.
    """
    if search_type not in ("account_id", "ip_address"):
        return {"error": f"Invalid search_type: {search_type!r}"}

    clean_terms = list({t.strip() for t in terms if t and t.strip()})
    if not clean_terms:
        return {"error": "No valid search terms"}

    base_dir = Path(data_base_dir) if data_base_dir else _DATA_BASE_DIR
    dates = _available_dates(base_dir)
    if not dates:
        return {"error": f"No analysis data found under {base_dir}"}
    dates = dates[:days]

    results_list: list[dict[str, Any]] = []

    for date_str in dates:
        day_dir = base_dir / date_str
        account_logins_all = _load_json(day_dir / _ACCOUNT_LOGINS_FILE)
        ip_to_accounts_all = _load_json(day_dir / _IP_MAPPING_FILE)
        if not account_logins_all or not ip_to_accounts_all:
            continue

        if search_type == "account_id":
            for server, server_logins in account_logins_all.items():
                server_ip_to_accs = ip_to_accounts_all.get(server, {})
                for term in clean_terms:
                    if term not in server_logins:
                        continue
                    for ip, count in server_logins[term].items():
                        others = [
                            acc for acc in server_ip_to_accs.get(ip, [])
                            if str(acc) != term
                        ]
                        results_list.append({
                            "kind": "account_id",
                            "search_term": term,
                            "date": date_str,
                            "server": server,
                            "login_ip": ip,
                            "login_count": count,
                            "_raw_correlated": others,  # pre-filter; used below
                        })

        else:  # ip_address
            for server, server_ip_to_accs in ip_to_accounts_all.items():
                for term in clean_terms:
                    if term in server_ip_to_accs:
                        results_list.append({
                            "kind": "ip_address",
                            "search_term": term,
                            "date": date_str,
                            "server": server,
                            "correlated_accounts": [
                                str(a) for a in server_ip_to_accs[term]
                            ],
                        })

    if not results_list:
        return {"not_found": f"未在最近 {days} 天内找到相关记录"}

    # Only account_id mode has the same-client_id filtering + enrichment step.
    if search_type == "ip_address":
        return {"results": results_list}

    # --- account_id post-processing: MySQL enrichment + same-client filter ---
    # Enrichment supplies client_id (CRM) + names. Filter: for each row,
    # drop correlated accounts where corr["client_id"] == search["client_id"];
    # only different CRM identities are listed (and labeled with name).
    from ..services import login_ip_enrichment_service

    def _strip_name(v: object | None) -> str | None:
        if v is None:
            return None
        s = str(v).strip().strip('"“”')
        return s or None

    all_ids: set[str] = set()
    for r in results_list:
        all_ids.add(r["search_term"])
        for acc in r.get("_raw_correlated", []):
            all_ids.add(str(acc))

    details, db_ok = login_ip_enrichment_service.get_account_details(all_ids)
    if not db_ok:
        # MySQL unreachable — same-client filter requires client_id from DB.
        return {"error": "无法从数据库获取账户详细信息，请检查数据库连接（DB_HOST / DB_USER / 从库权限）"}

    enriched: list[dict[str, Any]] = []
    for r in results_list:
        search_info = details.get(r["search_term"])
        if not search_info:
            # Search term itself isn't a real MT account — skip (matches legacy).
            continue
        search_client_id = search_info["client_id"]

        # Show only other CRM users (different userId / users.id) on the same IP.
        filtered: list[dict[str, Any]] = []
        for acc in r["_raw_correlated"]:
            acc_str = str(acc)
            corr_info = details.get(acc_str)
            if not corr_info:
                continue
            if corr_info["client_id"] == search_client_id:
                continue
            filtered.append({
                "login": acc_str,
                "first_name": _strip_name(corr_info.get("first_name")),
                "last_name": _strip_name(corr_info.get("last_name")),
            })

        if not filtered:
            continue  # nothing interesting for this row

        enriched.append({
            "kind": "account_id",
            "search_term": r["search_term"],
            "search_term_first_name": _strip_name(search_info.get("first_name")),
            "search_term_last_name": _strip_name(search_info.get("last_name")),
            "client_id": str(search_client_id) if search_client_id else None,
            "date": r["date"],
            "server": r["server"],
            "login_ip": r["login_ip"],
            "login_count": r["login_count"],
            "correlated_accounts": filtered,
        })

    if not enriched:
        return {"not_found": f"在最近 {days} 天内未找到其他不同客户主体名下的关联账户"}
    return {"results": enriched}
