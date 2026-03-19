"""
Centralized Logging Configuration with File Persistence

Features:
- Unified log format across all modules
- Supports LOG_LEVEL environment variable (DEBUG/INFO/WARNING/ERROR)
- Integrates Trace ID for request tracing
- Docker-friendly: outputs to stdout/stderr
- File persistence with daily rotation (30 days retention)

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
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Context variable for request trace ID (thread-safe in async context)
# Fresh grad note: ContextVar ensures each request has its own trace_id,
# even when multiple requests are processed concurrently
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)


class TraceIDFilter(logging.Filter):
    """
    Logging filter that injects trace_id into log records.
    
    Fresh grad note: 
    - Filter allows adding custom fields to every log message
    - This enables request tracing across all log entries
    """
    def filter(self, record: logging.LogRecord) -> bool:
        # Get trace_id from context, use "-" if not set (e.g., startup logs)
        record.trace_id = trace_id_var.get() or "-"
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
    # Format: [timestamp] [LEVEL] [trace_id] [module:lineno] - message
    log_format = (
        "[%(asctime)s] [%(levelname)s] [%(trace_id)s] "
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
    
    # === File Handler with Daily Rotation ===
    # Docker uses /app/logs; local dev falls back to ./logs
    log_dir = Path(os.environ.get("LOG_FILE_DIR", "/app/logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        log_dir = Path(__file__).resolve().parents[2] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
    
    file_handler = TimedRotatingFileHandler(
        filename=log_dir / "backend.log",
        when="midnight",           # Rotate at midnight
        interval=1,                # Every 1 day
        backupCount=30,            # Keep 30 days of logs
        encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(TraceIDFilter())
    file_handler.suffix = "%Y-%m-%d"  # File suffix format: backend.log.2026-01-29
    
    # === Configure Root Logger ===
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove any existing handlers to avoid duplicates on reload
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    
    # === Suppress Noisy Third-Party Loggers ===
    # These libraries generate too much noise at INFO level
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
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
