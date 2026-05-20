"""
Async CSV export service for Client Return Rate page.

Flow:
1) Create task in SQLite.
2) Execute export in background thread.
3) Poll task status from API.
4) Download generated CSV when task succeeds.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.client_return_export_db import (
    create_export_task,
    delete_export_task,
    get_export_task,
    init_client_return_export_db,
    list_tasks_for_cleanup,
    update_task_expired,
    update_task_failed,
    update_task_progress,
    update_task_running,
    update_task_succeeded,
)
from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.client_return_service import get_client_return_rate_data

logger = get_logger(__name__)

_HK_TZ = timezone(timedelta(hours=8))

_CSV_FIELDS = [
    "client_id",
    "country",
    "zipcode",
    "is_akcm",
    "has_usdt_tag",
    "net_deposit_hist",
    "net_deposit_month",
    "equity",
    "profit_hist",
    "month_trade_profit",
    "avg_daily_equity",
    "return_on_avg_equity",
    "adj_0_2000",
    "adj_2000_5000",
    "adj_5000_50000",
    "adj_50000_plus",
    "return_non_adjusted",
    "deposits_90d",
    "return_neg_adjusted",
]

_settings = get_settings()
_executor = ThreadPoolExecutor(
    max_workers=max(1, int(getattr(_settings, "CLIENT_RETURN_EXPORT_MAX_WORKERS", 1))),
    thread_name_prefix="client-return-export",
)


def _now_hk_iso() -> str:
    return datetime.now(_HK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _export_dir() -> Path:
    settings = get_settings()
    configured_raw = getattr(settings, "CLIENT_RETURN_EXPORT_DIR", "")
    configured = configured_raw.strip() if isinstance(configured_raw, str) else ""
    if configured:
        return Path(configured)
    return settings.repo_root / "backend" / "data" / "exports" / "client-return-rate"


def _to_status_payload(task: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "task_id": task["task_id"],
        "status": task["status"],
        "progress": int(task.get("progress") or 0),
        "row_count": int(task.get("row_count") or 0),
        "file_size_bytes": int(task.get("file_size_bytes") or 0)
        if task.get("file_size_bytes") is not None
        else None,
        "error_message": task.get("error_message"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "expires_at": task.get("expires_at"),
        "download_url": None,
    }
    if task.get("status") == "succeeded" and task.get("file_path"):
        payload["download_url"] = (
            f"/api/v1/client-return-rate/export/tasks/{task['task_id']}/download"
        )
    return payload


def _cleanup_tasks() -> None:
    """
    Cleanup expired files and stale task rows.

    This is lazy cleanup, triggered by create/status/download requests.
    """
    settings = get_settings()
    now = datetime.now(_HK_TZ)
    expired_before = now.strftime("%Y-%m-%d %H:%M:%S")
    cleanup_days = int(getattr(settings, "CLIENT_RETURN_EXPORT_CLEANUP_DAYS", 7))
    finished_before = (now - timedelta(days=cleanup_days)).strftime("%Y-%m-%d %H:%M:%S")
    tasks = list_tasks_for_cleanup(expired_before=expired_before, finished_before=finished_before)
    for task in tasks:
        task_id = task["task_id"]
        file_path = task.get("file_path")
        status = task.get("status")
        expired_at = task.get("expires_at")
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            # Mark only if it is an unexpired success task reaching expiration.
            if status == "succeeded" and expired_at and expired_at < expired_before:
                update_task_expired(task_id, finished_at=_now_hk_iso())
        except Exception:
            logger.warning("Failed to cleanup file for task=%s", task_id, exc_info=True)

        # Remove long-retained terminal tasks.
        created_at = task.get("created_at") or ""
        if created_at < finished_before:
            delete_export_task(task_id)


def _build_csv_file(task_id: str, rows: list[dict[str, Any]]) -> tuple[Path, int]:
    output_dir = _export_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"client_return_rate_{task_id}_{datetime.now(_HK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
    file_path = output_dir / file_name

    with file_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        total = len(rows)
        for idx, row in enumerate(rows, start=1):
            writer.writerow({k: row.get(k) for k in _CSV_FIELDS})
            # Keep progress smooth without heavy DB writes.
            if idx % 1000 == 0 and total > 0:
                progress = 40 + int((idx / total) * 55)
                update_task_progress(task_id, progress)

    size = file_path.stat().st_size if file_path.exists() else 0
    return file_path, size


def _params_hash(params: dict[str, Any]) -> str:
    """Hash all effective export params for observability and cache semantics."""
    normalized = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _run_export_task(task_id: str) -> None:
    update_task_running(task_id, started_at=_now_hk_iso())
    try:
        task = get_export_task(task_id)
        if not task:
            logger.warning("Export task missing while running: %s", task_id)
            return

        params = json.loads(task["params_json"])
        update_task_progress(task_id, 15)

        query_result = get_client_return_rate_data(
            page=1,
            page_size=5000,
            sort_by=params.get("sort_by", "month_trade_profit"),
            sort_order=params.get("sort_order", "desc"),
            search=params.get("search"),
            deposit_bucket=params.get("deposit_bucket"),
            month_start=params.get("month_start"),
            month_end=params.get("month_end"),
            close_time_start=params.get("close_time_start"),
            include_avg_equity=bool(params.get("include_avg_equity", True)),
            country_filter=params.get("country_filter"),
            akcm_filter=params.get("akcm_filter"),
            usdt_filter=params.get("usdt_filter"),
            return_all=True,
            use_cache=False,
        )
        rows = query_result.get("data", [])
        row_count = len(rows)
        update_task_progress(task_id, 35)

        settings = get_settings()
        max_rows = int(getattr(settings, "CLIENT_RETURN_EXPORT_MAX_ROWS", 200000))
        if row_count > max_rows:
            raise ValueError(
                f"导出结果 {row_count} 行，超过上限 {max_rows}。请缩小时间范围后重试。"
            )

        file_path, file_size = _build_csv_file(task_id, rows)

        expires_hours = int(getattr(settings, "CLIENT_RETURN_EXPORT_EXPIRE_HOURS", 24))
        finished_at = _now_hk_iso()
        expires_at = (
            datetime.now(_HK_TZ) + timedelta(hours=expires_hours)
        ).strftime("%Y-%m-%d %H:%M:%S")
        update_task_succeeded(
            task_id=task_id,
            row_count=row_count,
            file_path=str(file_path),
            file_size_bytes=file_size,
            finished_at=finished_at,
            expires_at=expires_at,
        )
        logger.info(
            "Client return export succeeded: task=%s rows=%d size=%d path=%s",
            task_id,
            row_count,
            file_size,
            file_path,
        )
    except Exception as exc:
        logger.exception("Client return export failed: task=%s", task_id)
        update_task_failed(task_id, error_message=str(exc), finished_at=_now_hk_iso())


def create_client_return_export_task(
    params: dict[str, Any],
    requested_ip: str | None,
) -> dict[str, Any]:
    """Create and enqueue an async export task."""
    init_client_return_export_db()
    _cleanup_tasks()

    task_id = uuid.uuid4().hex
    created_at = _now_hk_iso()
    params_hash = _params_hash(params)
    create_export_task(
        task_id=task_id,
        params_json=json.dumps(params, ensure_ascii=False),
        params_hash=params_hash,
        requested_ip=requested_ip,
        created_at=created_at,
    )
    _executor.submit(_run_export_task, task_id)
    logger.info(
        "Client return export queued: task=%s ip=%s params_hash=%s",
        task_id,
        requested_ip,
        params_hash,
    )
    return {"task_id": task_id, "status": "queued", "created_at": created_at}


def get_client_return_export_task(task_id: str) -> dict[str, Any] | None:
    """Get task status payload. Also performs lazy expiration checks."""
    init_client_return_export_db()
    _cleanup_tasks()
    task = get_export_task(task_id)
    if not task:
        return None

    expires_at = task.get("expires_at")
    if task.get("status") == "succeeded" and expires_at and expires_at < _now_hk_iso():
        file_path = task.get("file_path")
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        update_task_expired(task_id, finished_at=_now_hk_iso())
        task = get_export_task(task_id)
        if not task:
            return None
    return _to_status_payload(task)


def get_client_return_export_file(task_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Return file path and status payload for download checks."""
    status = get_client_return_export_task(task_id)
    if not status:
        return None, None

    task = get_export_task(task_id)
    if not task:
        return None, status
    raw_file_path = task.get("file_path")
    if not raw_file_path:
        return None, status

    resolved = Path(raw_file_path).resolve()
    allowed_root = _export_dir().resolve()
    # Ensure file stays under export directory to avoid any path traversal risk.
    if not str(resolved).startswith(str(allowed_root)):
        logger.warning("Blocked export download outside allowed dir: task=%s path=%s", task_id, resolved)
        return None, status
    if not resolved.exists():
        return None, status
    return str(resolved), status
