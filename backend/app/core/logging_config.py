"""
Centralized Logging Configuration with File Persistence

Features:
- Unified log format across all modules
- Supports LOG_LEVEL environment variable (DEBUG/INFO/WARNING/ERROR)
- Integrates Trace ID for request tracing
- Docker-friendly: outputs to stdout/stderr
- File persistence; daily rotation is delegated to the host's logrotate
  (see the "File Handler" section below and docs/architecture/logging-system.md)

Usage:
    from app.core.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Something happened")
"""

import logging
import sys
import os
from contextvars import ContextVar
from typing import Optional
from logging.handlers import WatchedFileHandler
from pathlib import Path

# Context variable for request trace ID (thread-safe in async context)
# Fresh grad note: ContextVar ensures each request has its own trace_id,
# even when multiple requests are processed concurrently
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)

# Email of the person behind the current request. Set by AuthMiddleware the
# moment a session resolves, stamped onto every log line by TraceIDFilter.
#
# Why the email string and not the SessionUser object: the log format only needs
# a short string, and holding an auth object here would make the logging module
# depend on the auth module — a circular import, since auth logs.
#
# Stays None for: requests before AuthMiddleware runs (the Trace middleware's own
# start/end pair), AUTH_ENABLED=false, and scheduler jobs — none of which have an
# operator, and inventing one would be worse than printing "-".
user_email_var: ContextVar[Optional[str]] = ContextVar("user_email", default=None)


class TraceIDFilter(logging.Filter):
    """
    Stamps request context — trace_id and operator email — onto every log record.

    The name is kept (it is already attached to both handlers and referenced in
    docs) but the job is now "request-context injector" rather than trace_id only.

    Fresh grad note:
    - Filter allows adding custom fields to every log message
    - This enables request tracing across all log entries
    - Both lookups use `or "-"`, so this filter cannot raise; a raising filter
      would swallow the log line it was supposed to decorate.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        # Get trace_id from context, use "-" if not set (e.g., startup logs)
        record.trace_id = trace_id_var.get() or "-"
        record.user = user_email_var.get() or "-"
        return True


def setup_logging(log_level: str = "INFO") -> None:
    """
    Initialize logging for the entire application.
    Should be called once at startup before any other imports.
    
    Args:
        log_level: One of DEBUG, INFO, WARNING, ERROR, CRITICAL
    
    Fresh grad note:
    - This function configures the root logger
    - All child loggers (created via get_logger) inherit this config
    - We use both console and file handlers for redundancy
    """
    # Normalize log level string to logging constant
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Define log format
    # Format: [timestamp] [LEVEL] [trace_id] [user] [module:lineno] - message
    #
    # The user column turns "the user says it was slow" into `grep their.email`
    # across EVERY log line, including the ones that will never carry an audit
    # row (queries, errors, timeouts). "-" means no authenticated subject —
    # see user_email_var for the three ways that happens.
    log_format = (
        "[%(asctime)s] [%(levelname)s] [%(trace_id)s] [%(user)s] "
        "[%(name)s:%(lineno)d] - %(message)s"
    )
    date_format = "%Y-%m-%d %H:%M:%S"
    
    # Create formatter
    formatter = logging.Formatter(log_format, datefmt=date_format)
    
    # === Console Handler ===
    # Docker collects logs from stdout/stderr automatically
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(TraceIDFilter())
    
    # === File Handler (rotation delegated to the host's logrotate) ===
    # Docker uses /app/logs; local dev falls back to ./logs.
    #
    # WHY NOT TimedRotatingFileHandler: production runs `uvicorn --workers 4`
    # (see backend/Dockerfile). Every worker re-imports app.main, so every
    # worker ran its own setup_logging() and built its own rotating handler —
    # 4 independent file descriptors and 4 independent rolloverAt clocks all
    # renaming/unlinking the same path at midnight. Measured on prod: 18 of 30
    # archives held cross-day data, 25 of 30 calendar days were shredded across
    # >=2 files, and doRollover()'s os.remove(dfn) can delete a whole day's
    # archive outright. Single-process dev (--reload) was the clean control:
    # 30/30 archives uncontaminated.
    #
    # WatchedFileHandler instead stat()s the path before each emit and reopens
    # when the inode changes. Rotation itself is handed to the host's logrotate
    # (deploy/logrotate/new-it-backend, `create` mode = rename → new inode), so
    # all 4 workers atomically follow the rename with no coordination at all.
    # Full rationale + install steps: docs/architecture/logging-system.md
    #
    # `except OSError` (not just PermissionError) covers a missing parent dir,
    # a read-only mount and a full disk too.
    log_dir = Path(os.environ.get("LOG_FILE_DIR", "/app/logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        try:
            log_dir = Path(__file__).resolve().parents[2] / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = None

    # setup_logging() runs at app.main module scope: raising here would fail the
    # import for every worker and put the container in a crash loop. A logging
    # problem must never take the process down — degrade to console-only, which
    # Docker still captures. Nothing in this block may raise.
    file_handler = None
    file_handler_error: Optional[str] = None
    if log_dir is None:
        file_handler_error = "log directory unavailable"
    else:
        try:
            file_handler = WatchedFileHandler(
                filename=log_dir / "backend.log",
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(TraceIDFilter())
        except OSError as e:
            file_handler = None
            file_handler_error = repr(e)

    # === Configure Root Logger ===
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove any existing handlers to avoid duplicates on reload
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    if file_handler is not None:
        root_logger.addHandler(file_handler)
    else:
        # Warn only after the console handler is wired up, so the message is
        # actually visible instead of going to logging's lastResort.
        logging.getLogger(__name__).warning(
            f"File logging disabled, continuing console-only: {file_handler_error}"
        )

    # === Suppress Noisy Third-Party Loggers ===
    # These libraries generate too much noise at INFO level
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # NOTE: uvicorn.error is deliberately NOT suppressed. Despite the name it is
    # uvicorn's general server logger — "Started server process", worker
    # start/stop/crash lines all ride on it. Silencing it hid a real incident:
    # prod logs showed "Logging initialized" 5 times but only 4 lifespan
    # startups, i.e. a worker that half-started and died with no diagnosable
    # trace. uvicorn.access (one INFO line per HTTP request) is the actual
    # noise source and stays at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # APScheduler's executor logs two INFO lines ("Running job" / "Job executed
    # successfully") for every single tick. With 3 per-minute jobs that is
    # ~6.4 lines/minute, measured at 64.9%-73.1% of the entire prod log volume,
    # against only 4 WARNING+ERROR lines across a whole day. No failure
    # visibility is lost: a job that raises is still reported by this same
    # logger at ERROR with its traceback. Only the executor is muted —
    # apscheduler.scheduler stays at INFO (~34 lines/day: jobs added/removed/
    # missed), which is genuinely useful.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    # Log startup confirmation
    root_logger.info(f"Logging initialized: level={log_level}, log_dir={log_dir}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given module.
    
    Args:
        name: Usually __name__ to get module-specific logger
    
    Returns:
        Configured logger instance
    
    Usage:
        from app.core.logging_config import get_logger
        logger = get_logger(__name__)
        logger.info("User logged in", extra={"user_id": 123})
        logger.error("Database connection failed")
        logger.exception("Unexpected error")  # Includes stack trace
    """
    return logging.getLogger(name)
