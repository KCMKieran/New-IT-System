"""
FTP / FTPS download service for the Login IP Monitor.

Responsible for **only** fetching each day's raw login log from the 3 MT
servers into a local temp dir. Parsing, DB writes, and email dispatch belong
to sibling services/jobs.

Called by
---------
- `scripts/backfill_login_ip.py`      — one-shot multi-day backfill
- `core/login_ip_scheduler.py`        — Phase 5 APScheduler daily job (06:00)
- `api/v1/routes/login_ip.py`         — Phase 6 manual "run-now" admin endpoint

Config
------
All 3 servers are read from environment variables (`LOGIN_IP_*`). Missing
required fields raise `RuntimeError` at `load_ftp_configs()` time — we don't
want the daily scheduler to silently skip a server and only notice via
missing JSON hours later.

FTPS quirk (DO NOT simplify)
----------------------------
MT4_Live2 speaks FTPS but rejects data-channel TLS sessions that are NOT
reused from the control channel. Stock `ftplib.FTP_TLS` calls `unwrap()` on
the data socket, which this vendor treats as a brand-new (invalid) session.

`FTP_TLS_IgnoreHost` + `ReusedSslSocket` below force session reuse and skip
the unwrap. Plain `FTP_TLS` has been tried several times by the legacy
project and always fails on MT4_Live2.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import ssl
from ftplib import FTP, FTP_TLS
from pathlib import Path
from ssl import SSLSocket
from typing import Literal

logger = logging.getLogger(__name__)

# The 3 servers we pull from. Keep as a module constant so callers (scheduler,
# admin API) can iterate in a predictable order.
SERVER_NAMES: tuple[str, ...] = ("MT4", "MT5", "MT4_Live2")
ServerName = Literal["MT4", "MT5", "MT4_Live2"]


# ---------------------------------------------------------------------------
# FTPS helpers — vendor workaround, see module docstring
# ---------------------------------------------------------------------------


class ReusedSslSocket(SSLSocket):
    """SSLSocket that skips unwrap() so the TLS session stays reusable."""

    def unwrap(self):  # noqa: D401 — match parent API intentionally
        pass


class FTP_TLS_IgnoreHost(FTP_TLS):
    """FTP_TLS that reuses the control-channel TLS session for data transfers."""

    def makepasv(self):
        # Some servers advertise an unroutable internal IP in PASV responses;
        # force-use the hostname we actually connected to.
        _, port = super().makepasv()
        return self.host, port

    def ntransfercmd(self, cmd, rest=None):
        conn, size = FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,
            )
            conn.__class__ = ReusedSslSocket
        return conn, size


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _env_bool(key: str, default: bool = False) -> bool:
    """Parse a loose boolean env var (`true/false/yes/no/1/0`)."""
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def load_ftp_configs() -> dict[str, dict]:
    """Build the 3-server config dict from environment variables.

    Returns `{server_name: config_dict}`. Raises `RuntimeError` immediately
    if any required field (`host` / `user` / `password`) is missing so the
    caller gets a loud failure rather than a silently skipped server.
    """
    configs = {
        "MT4": {
            "server": os.environ.get("LOGIN_IP_MT4_HOST"),
            "port": int(os.environ.get("LOGIN_IP_MT4_PORT", "22")),
            "username": os.environ.get("LOGIN_IP_MT4_USER"),
            "password": os.environ.get("LOGIN_IP_MT4_PASSWORD"),
            "remote_dir": os.environ.get("LOGIN_IP_MT4_REMOTE_DIR", "/Mt4log"),
            "use_ftps": _env_bool("LOGIN_IP_MT4_USE_FTPS", False),
        },
        "MT5": {
            "server": os.environ.get("LOGIN_IP_MT5_HOST"),
            "port": int(os.environ.get("LOGIN_IP_MT5_PORT", "22")),
            "username": os.environ.get("LOGIN_IP_MT5_USER"),
            "password": os.environ.get("LOGIN_IP_MT5_PASSWORD"),
            "remote_dir": os.environ.get("LOGIN_IP_MT5_REMOTE_DIR", "/MT5Main"),
            "use_ftps": _env_bool("LOGIN_IP_MT5_USE_FTPS", False),
        },
        "MT4_Live2": {
            "server": os.environ.get("LOGIN_IP_MT4_LIVE2_HOST"),
            "port": int(os.environ.get("LOGIN_IP_MT4_LIVE2_PORT", "22")),
            "username": os.environ.get("LOGIN_IP_MT4_LIVE2_USER"),
            "password": os.environ.get("LOGIN_IP_MT4_LIVE2_PASSWORD"),
            "remote_dir": os.environ.get("LOGIN_IP_MT4_LIVE2_REMOTE_DIR", "/MT4"),
            "use_ftps": _env_bool("LOGIN_IP_MT4_LIVE2_USE_FTPS", True),
        },
    }

    missing: list[str] = []
    for name, cfg in configs.items():
        for key in ("server", "username", "password"):
            if not cfg.get(key):
                missing.append(f"{name}.{key}")
    if missing:
        raise RuntimeError(
            "Incomplete Login IP FTP config, missing: "
            + ", ".join(missing)
            + ". Please check backend/.env for LOGIN_IP_* keys."
        )
    return configs


# ---------------------------------------------------------------------------
# Per-server download (internal)
# ---------------------------------------------------------------------------


def _download_one_server(
    server_name: str,
    cfg: dict,
    target_date: str,
    local_dir: Path,
    timeout: int = 60,
) -> Path | None:
    """Download `<target_date>.log` from one MT server into `local_dir`.

    File naming convention (mirrors legacy project for JSON-parser compatibility):
        <local_dir>/<YYYYMMDD>_<server_name>.log

    Returns the local Path on success, or None on any failure. Never raises —
    per-server isolation is a hard requirement; one bad server must not
    prevent the other two from being fetched.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / f"{target_date}_{server_name}.log"

    # Idempotency: if the .log is already here and non-empty, skip. Important
    # for backfill re-runs and for scheduler jobs that partially succeeded.
    if local_path.exists() and local_path.stat().st_size > 0:
        logger.info("[%s] .log already present, skip download (%s)", server_name, local_path.name)
        return local_path

    ftp = None
    try:
        logger.info(
            "[%s] connecting %s://%s:%s (remote_dir=%s)",
            server_name,
            "ftps" if cfg["use_ftps"] else "ftp",
            cfg["server"],
            cfg["port"],
            cfg["remote_dir"],
        )
        if cfg["use_ftps"]:
            FTP_TLS.port = cfg["port"]
            ftp = FTP_TLS_IgnoreHost(cfg["server"], timeout=timeout)
            ftp.ssl_version = ssl.PROTOCOL_TLS
            ftp.auth()
            ftp.login(cfg["username"], cfg["password"])
            ftp.prot_p()  # encrypt the data channel
        else:
            ftp = FTP()
            ftp.connect(cfg["server"], cfg["port"], timeout=timeout)
            ftp.login(cfg["username"], cfg["password"])
        ftp.set_pasv(True)

        if cfg["remote_dir"]:
            ftp.cwd(cfg["remote_dir"])

        remote_name = f"{target_date}.log"
        logger.info("[%s] downloading %s ...", server_name, remote_name)
        with open(local_path, "wb") as fp:
            ftp.retrbinary(f"RETR {remote_name}", fp.write)

        size_mb = local_path.stat().st_size / (1024 * 1024)
        logger.info("[%s] downloaded %.1f MB -> %s", server_name, size_mb, local_path.name)
        return local_path

    except Exception as exc:
        logger.warning("[%s] DOWNLOAD FAILED: %s", server_name, exc)
        # Remove half-written file so the downstream analyzer doesn't try to
        # parse a truncated/corrupt log and produce garbage JSON.
        if local_path.exists():
            try:
                local_path.unlink()
            except OSError:
                pass
        return None

    finally:
        if ftp is not None:
            try:
                if hasattr(ftp, "quit"):
                    ftp.quit()
                else:
                    ftp.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def download_daily_logs(
    target_date: str,
    base_dir: Path | str,
    timeout: int = 60,
) -> dict[str, bool]:
    """Download `<target_date>.log` from all 3 servers into `<base_dir>/<target_date>/`.

    This is the single entrypoint for the scheduler job, the admin "run-now"
    API, and the backfill script. All three should route through here so
    filename, directory layout, and logging style stay consistent.

    Args:
        target_date: YYYYMMDD string, tz-agnostic — caller is responsible for
            converting to Asia/Shanghai "yesterday" if that's the intent.
            (See login-ip_migration.md §9 pitfall #6 re timezone.)
        base_dir: The parent directory under which a `<target_date>/` subdir
            is created. Typical value: `backend/data/login_ip/tmp`.
        timeout: Socket timeout per server in seconds.

    Returns:
        `{server_name: True/False}` — True means the .log is on disk and
        non-empty. Callers decide whether partial success is acceptable.
    """
    base_dir = Path(base_dir)
    local_dir = base_dir / target_date

    configs = load_ftp_configs()
    results: dict[str, bool] = {}
    for name in SERVER_NAMES:
        path = _download_one_server(name, configs[name], target_date, local_dir, timeout=timeout)
        results[name] = path is not None
    return results


def cleanup_old_log_dirs(base_dir: Path | str, days_to_keep: int = 7) -> int:
    """Delete `*.log` files in `<base_dir>/YYYYMMDD/` directories older than
    `days_to_keep`, and remove the directory itself if it ends up empty.

    Does NOT touch JSON analysis results (those live under the sibling date
    directories, not under `base_dir` if callers follow the convention).

    The legacy project also cleaned `cron_*.log` files, but the new platform
    routes all logs through `logging`, so no cron files exist here.

    Returns the number of .log files deleted (for the caller's summary log).

    Silent on empty workloads — this runs daily and most days will have
    nothing to delete; we don't want to spam the logs.
    """
    base_dir = Path(base_dir)
    if not base_dir.is_dir():
        logger.debug("cleanup: base_dir missing, skip: %s", base_dir)
        return 0

    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days_to_keep)).date()

    deleted_files = 0
    removed_dirs = 0

    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        # Only touch directories whose name looks like YYYYMMDD — leave
        # unrelated subdirs alone.
        try:
            dir_date = _dt.datetime.strptime(entry.name, "%Y%m%d").date()
        except ValueError:
            continue

        if dir_date >= cutoff:
            continue

        for log_file in entry.glob("*.log"):
            try:
                log_file.unlink()
                logger.info("cleanup: deleted %s/%s", entry.name, log_file.name)
                deleted_files += 1
            except OSError as exc:
                logger.warning("cleanup: failed to delete %s: %s", log_file, exc)

        try:
            # Remove the directory ONLY if it's completely empty. Leftover
            # files (shouldn't happen in practice) are preserved for review.
            entry.rmdir()
            logger.info("cleanup: removed empty dir %s", entry.name)
            removed_dirs += 1
        except OSError:
            pass

    if deleted_files or removed_dirs:
        logger.info(
            "cleanup summary: %d .log file(s) deleted, %d empty dir(s) removed",
            deleted_files,
            removed_dirs,
        )
    return deleted_files
