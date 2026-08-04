"""
Async CSV export service for Login IP Monitor search results.

Mirrors the architecture of `client_return_export_service`:
1. `POST /login-ip/export/tasks` inserts a queued row and submits to a
   module-level `ThreadPoolExecutor`.
2. The worker re-runs `login_ip_search_service.perform_search()` with the
   original params and writes the result to a CSV file under
   `backend/data/exports/login-ip/`. Each correlated account gets its own CSV
   row (see `_explode_correlated`) — the on-screen grid keeps them collapsed
   into one cell, the export does not.
3. `GET /.../tasks/{id}` returns JSON status; `.../download` serves the file.
4. Lazy cleanup on every create/status/download:
   - succeeded tasks past `expires_at` → marked expired + file removed
   - any terminal task older than retention window → row + file removed

The task table sits inside `login_ip.db` (`login_ip_export_tasks`) to avoid
yet another SQLite file on disk.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core import login_ip_db
from app.services import login_ip_search_service

logger = logging.getLogger(__name__)

_HK_TZ = timezone(timedelta(hours=8))
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_EXPORT_DIR = _BACKEND_ROOT / "data" / "exports" / "login-ip"

# Single-worker by default — search is cheap (reads JSONs) but we don't want
# many concurrent exports pounding MySQL for enrichment.
_MAX_WORKERS = int(os.getenv("LOGIN_IP_EXPORT_MAX_WORKERS", "1"))
_EXPIRE_HOURS = int(os.getenv("LOGIN_IP_EXPORT_EXPIRE_HOURS", "24"))
_CLEANUP_DAYS = int(os.getenv("LOGIN_IP_EXPORT_CLEANUP_DAYS", "7"))

_executor = ThreadPoolExecutor(
    max_workers=max(1, _MAX_WORKERS), thread_name_prefix="login-ip-export"
)


def _now_hk_iso() -> str:
    return datetime.now(_HK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _status_payload(task: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "task_id": task["task_id"],
        "status": task["status"],
        "progress": int(task.get("progress") or 0),
        "row_count": int(task["row_count"]) if task.get("row_count") is not None else None,
        "error_message": task.get("error_message"),
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
        "download_url": None,
    }
    if task.get("status") == "succeeded" and task.get("file_path"):
        payload["download_url"] = (
            f"/api/v1/login-ip/export/tasks/{task['task_id']}/download"
        )
    return payload


def _cleanup() -> None:
    """Lazy cleanup called on every API entry point. Never raises."""
    try:
        now = datetime.now(_HK_TZ)
        retention_cutoff = (now - timedelta(days=_CLEANUP_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
        rows = login_ip_db.list_export_tasks_for_cleanup(now_iso, retention_cutoff)
        for row in rows:
            fp = row.get("file_path")
            if fp:
                try:
                    Path(fp).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("cleanup: unlink %s failed: %s", fp, exc)
            # Mark as expired rather than deleting, so the UI can explain
            # why a task that was "succeeded" suddenly has no file. Rows older
            # than retention cutoff are fully removed.
            if (
                row["status"] == "succeeded"
                and row.get("expires_at")
                and row["expires_at"] < now_iso
            ):
                login_ip_db.update_export_task_expired(row["task_id"], now_iso)
            if row.get("created_at") and row["created_at"] < retention_cutoff:
                login_ip_db.delete_export_task(row["task_id"])
    except Exception as exc:
        logger.warning("export cleanup failed (non-fatal): %s", exc)


def create_export_task(
    search_type: str,
    terms: list[str],
    days: int,
    requested_ip: str | None = None,
) -> dict[str, Any]:
    """Insert a queued task and submit the worker. Returns the status payload."""
    _cleanup()
    task_id = uuid.uuid4().hex
    params = {"search_type": search_type, "terms": terms, "days": days}
    login_ip_db.create_export_task(
        task_id=task_id,
        params_json=json.dumps(params, ensure_ascii=False),
        requested_ip=requested_ip,
        created_at=_now_hk_iso(),
    )
    _executor.submit(_run_task, task_id)
    task = login_ip_db.get_export_task(task_id)
    return _status_payload(task or {"task_id": task_id, "status": "queued"})


def get_task_status(task_id: str) -> dict[str, Any] | None:
    _cleanup()
    task = login_ip_db.get_export_task(task_id)
    return _status_payload(task) if task else None


def resolve_download_path(task_id: str) -> tuple[int, str | Path, str | None]:
    """Return (http_status, payload, filename).

    - 200 / Path / filename → stream the file
    - 404 / message / None  → task or file missing
    - 409 / message / None  → task not finished yet
    - 410 / message / None  → task expired
    """
    _cleanup()
    task = login_ip_db.get_export_task(task_id)
    if not task:
        return 404, "Export task not found", None
    status = task["status"]
    if status == "queued" or status == "running":
        return 409, f"Export task is {status}, not ready yet", None
    if status == "failed":
        return 409, task.get("error_message") or "Export failed", None
    if status == "expired":
        return 410, "Export file has expired", None
    if status == "succeeded":
        fp = task.get("file_path")
        if not fp or not Path(fp).exists():
            return 410, "Export file no longer exists on disk", None
        filename = Path(fp).name
        return 200, Path(fp), filename
    return 500, f"Unknown task status: {status}", None


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


# One correlated account per CSV row ("exploded"), so Excel can filter/sort/
# pivot on the peer account. The base columns repeat across the rows that
# belong to the same (search_term, date, server, IP) match; correlated_index /
# correlated_total let a reader tell where one match ends and the next begins.
_CSV_HEADERS_ACCOUNT = [
    "search_term",
    "search_term_first_name",
    "search_term_last_name",
    "client_id",
    "date",
    "server",
    "login_ip",
    "login_count",
    "correlated_index",
    "correlated_total",
    "correlated_login",
    "correlated_last_name",
    "correlated_first_name",
]
_CSV_HEADERS_IP = [
    "search_term",
    "date",
    "server",
    "correlated_index",
    "correlated_total",
    "correlated_login",
]


def _explode_correlated(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Fan one search result out into one dict per correlated account.

    A row with no correlated accounts still yields exactly one output row with
    the correlated_* columns left blank — otherwise a date/IP that was matched
    but had no peers would silently vanish from the export (only reachable in
    ip_address mode; account_id rows are already filtered upstream).
    """
    base = {k: v for k, v in row.items() if k != "correlated_accounts"}
    corr = row.get("correlated_accounts")
    items = corr if isinstance(corr, list) else []
    total = len(items)

    if total == 0:
        return [{**base, "correlated_index": "", "correlated_total": 0}]

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        cells: dict[str, Any] = {
            "correlated_index": idx,
            "correlated_total": total,
        }
        if isinstance(item, dict):
            # account_id mode: enrichment gave us CRM names alongside the login.
            cells["correlated_login"] = str(item.get("login", ""))
            cells["correlated_last_name"] = (item.get("last_name") or "").strip()
            cells["correlated_first_name"] = (item.get("first_name") or "").strip()
        else:
            # ip_address mode: raw account ids, no enrichment.
            cells["correlated_login"] = str(item)
        out.append({**base, **cells})
    return out


def _run_task(task_id: str) -> None:
    task = login_ip_db.get_export_task(task_id)
    if not task:
        logger.error("run_task: task %s not found", task_id)
        return

    params = json.loads(task["params_json"])
    login_ip_db.update_export_task_running(task_id, _now_hk_iso())

    try:
        login_ip_db.update_export_task_progress(task_id, 20)
        result = login_ip_search_service.perform_search(
            search_type=params["search_type"],
            terms=params["terms"],
            days=params["days"],
        )
        if "error" in result:
            raise RuntimeError(result["error"])

        rows = result.get("results") or []
        login_ip_db.update_export_task_progress(task_id, 70)

        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(_HK_TZ).strftime("%Y%m%d_%H%M%S")
        file_path = _EXPORT_DIR / f"login_ip_search_{task_id}_{stamp}.csv"

        if params["search_type"] == "account_id":
            headers = _CSV_HEADERS_ACCOUNT
        else:
            headers = _CSV_HEADERS_IP

        written = 0
        with file_path.open("w", encoding="utf-8-sig", newline="") as f:
            # utf-8-sig so Excel opens Chinese names correctly without mojibake.
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                for out_row in _explode_correlated(dict(r)):
                    w.writerow(out_row)
                    written += 1

        size = file_path.stat().st_size
        expires = (
            datetime.now(_HK_TZ) + timedelta(hours=_EXPIRE_HOURS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        login_ip_db.update_export_task_succeeded(
            task_id=task_id,
            # CSV rows, not search matches — one match now spans N rows, and the
            # UI reports this number as "已下载 N 行".
            row_count=written,
            file_path=str(file_path),
            file_size_bytes=size,
            finished_at=_now_hk_iso(),
            expires_at=expires,
        )
        logger.info(
            "export %s succeeded: %d matches -> %d csv rows, %s",
            task_id,
            len(rows),
            written,
            file_path,
        )

    except Exception as exc:  # noqa: BLE001 — must catch all to record failure
        logger.exception("export task %s failed", task_id)
        login_ip_db.update_export_task_failed(task_id, str(exc), _now_hk_iso())
