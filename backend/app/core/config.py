from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv


# Ensure .env is loaded for local development
load_dotenv()


class Settings:
    # Database
    DB_HOST: str | None
    DB_USER: str | None
    DB_PASSWORD: str | None
    DB_NAME: str | None
    DB_PORT: int
    DB_CHARSET: str
    FXBACK_DB_NAME: str | None

    # MySQL for ETL (source)
    MYSQL_HOST: str | None
    MYSQL_USER: str | None
    MYSQL_PASSWORD: str | None
    MYSQL_DATABASE: str | None
    MYSQL_PORT: int
    MYSQL_SSL_CA: str | None

    # PostgreSQL for reporting (target)
    POSTGRES_HOST: str | None
    POSTGRES_USER: str | None
    POSTGRES_PASSWORD: str | None
    POSTGRES_DBNAME: str | None
    POSTGRES_PORT: int

    # Risk-V2 case layer (OPT-0047): dedicated database + least-privilege
    # account on the SAME Azure PG flexible server as the reporting DB
    # (host/port reuse POSTGRES_HOST/POSTGRES_PORT).
    RISK_CASES_PG_DBNAME: str | None
    RISK_CASES_PG_USER: str | None
    RISK_CASES_PG_PASSWORD: str | None

    # Paths (resolved relative to repo root by default)
    PARQUET_DIR: str | None
    PUBLIC_EXPORT_DIR: str | None

    # CORS
    CORS_ORIGINS: List[str]

    # SMTP (email sending)
    SMTP_SERVER: str | None
    SMTP_PORT: int
    SMTP_USERNAME: str | None
    SMTP_PASSWORD: str | None

    # Logging
    LOG_LEVEL: str

    # Client Return Rate async export
    CLIENT_RETURN_EXPORT_DIR: str | None
    CLIENT_RETURN_EXPORT_EXPIRE_HOURS: int
    CLIENT_RETURN_EXPORT_MAX_ROWS: int
    CLIENT_RETURN_EXPORT_MAX_WORKERS: int
    CLIENT_RETURN_EXPORT_CLEANUP_DAYS: int

    # ROACE precompute scheduler (M2 / OPT-0006)
    CLIENT_ROACE_SCHEDULER_ENABLED: bool
    CLIENT_ROACE_REFRESH_HOUR: int
    CLIENT_ROACE_REFRESH_MINUTE: int

    # View profiles (OPT-0035): device-ids allowed to force-release a stuck claim
    VIEW_PROFILES_ADMIN_DEVICES: set[str]


    # Alert mail center (OPT-0042/0043): recipient domain allowlist
    ALERT_MAIL_ALLOWED_DOMAINS: set[str]

    def __init__(self) -> None:
        self.DB_HOST = os.environ.get("DB_HOST")
        self.DB_USER = os.environ.get("DB_USER")
        self.DB_PASSWORD = os.environ.get("DB_PASSWORD")
        self.DB_NAME = os.environ.get("DB_NAME")
        self.DB_PORT = int(os.environ.get("DB_PORT", "3306"))
        self.DB_CHARSET = os.environ.get("DB_CHARSET", "utf8mb4")
        
        self.FXBACK_DB_NAME = os.environ.get("FXBACK_DB_NAME")

        # MySQL (ETL 源库)
        self.MYSQL_HOST = os.environ.get("MYSQL_HOST")
        self.MYSQL_USER = os.environ.get("MYSQL_USER")
        self.MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
        self.MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE")
        self.MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
        self.MYSQL_SSL_CA = os.environ.get("MYSQL_SSL_CA")
        self.MYSQL_DATABASE_FXBACKOFFICE = os.environ.get("MYSQL_DATABASE_FXBACKOFFICE", "fxbackoffice")
        # Dedicated host for client-return-rate page; falls back to MYSQL_HOST
        self.MYSQL_HOST_PRIMARY = os.environ.get("MYSQL_HOST_PRIMARY") or self.MYSQL_HOST

        # PostgreSQL (报表库)
        self.POSTGRES_HOST = os.environ.get("POSTGRES_HOST")
        self.POSTGRES_USER = os.environ.get("POSTGRES_USER")
        self.POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD")
        self.POSTGRES_DBNAME = os.environ.get("POSTGRES_DBNAME")
        self.POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))

        # Risk-V2 case layer (OPT-0047)
        self.RISK_CASES_PG_DBNAME = os.environ.get("RISK_CASES_PG_DBNAME")
        self.RISK_CASES_PG_USER = os.environ.get("RISK_CASES_PG_USER")
        self.RISK_CASES_PG_PASSWORD = os.environ.get("RISK_CASES_PG_PASSWORD")

        self.PARQUET_DIR = os.environ.get("PARQUET_DIR")
        self.PUBLIC_EXPORT_DIR = os.environ.get("PUBLIC_EXPORT_DIR")

        self.CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

        # SMTP (email sending)
        self.SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.office365.com")
        self.SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
        self.SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
        self.SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

        # Logging configuration
        # Options: DEBUG, INFO, WARNING, ERROR, CRITICAL
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

        # Client Return Rate async CSV export
        self.CLIENT_RETURN_EXPORT_DIR = os.environ.get("CLIENT_RETURN_EXPORT_DIR")
        self.CLIENT_RETURN_EXPORT_EXPIRE_HOURS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_EXPIRE_HOURS", "24")
        )
        self.CLIENT_RETURN_EXPORT_MAX_ROWS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_MAX_ROWS", "200000")
        )
        self.CLIENT_RETURN_EXPORT_MAX_WORKERS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_MAX_WORKERS", "1")
        )
        self.CLIENT_RETURN_EXPORT_CLEANUP_DAYS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_CLEANUP_DAYS", "7")
        )

        # ROACE precompute (OPT-0006). Nightly cron writes avg_daily_equity into
        # backend/data/client_roace.db so the web request doesn't have to join
        # stats_balances (19M rows) on every hit.
        self.CLIENT_ROACE_SCHEDULER_ENABLED = (
            os.environ.get("CLIENT_ROACE_SCHEDULER_ENABLED", "false").lower() == "true"
        )
        self.CLIENT_ROACE_REFRESH_HOUR = int(
            os.environ.get("CLIENT_ROACE_REFRESH_HOUR", "6")
        )
        self.CLIENT_ROACE_REFRESH_MINUTE = int(
            os.environ.get("CLIENT_ROACE_REFRESH_MINUTE", "0")
        )

        # API Key for protecting /api/* endpoints (None = skip validation, for dev)
        self.API_KEY = os.environ.get("API_KEY")

        # Gap Trade → CRM risk tag (OPT-0032). Dedicated credentials, isolated
        # from other CRM integrations. .strip() defends against stray
        # whitespace in .env values (a leading space once shipped here and
        # only worked because both dotenv and compose happen to trim it).
        self.CRM_RISK_API_URL = (os.environ.get("CRM_RISK_API_URL") or "").strip()
        self.CRM_RISK_API_TOKEN = (os.environ.get("CRM_RISK_API_TOKEN") or "").strip()
        # Per-round digest recipients (comma-separated). Every tag change
        # (tagged / failed / skipped_cid) is emailed to this list.
        self.CRM_RISK_MAIL_TO = ",".join(
            a.strip()
            for a in os.environ.get("CRM_RISK_MAIL_TO", "").split(",")
            if a.strip()
        )

        # View profiles (OPT-0035): comma-separated device-ids allowed to
        # force-release a claim stuck on a lost device-id. The device-id is shown
        # (and copyable) on the Settings page. Empty = nobody can force-release.
        self.VIEW_PROFILES_ADMIN_DEVICES = {
            d.strip()
            for d in os.environ.get("VIEW_PROFILES_ADMIN_DEVICES", "").split(",")
            if d.strip()
        }


        # Alert mail center (OPT-0042/0043): server-side recipient domain
        # allowlist. Subscription mail_to/mail_cc and test-send recipients may
        # only target mailboxes in these domains — anyone holding the frontend
        # API key could otherwise create a subscription (or test-send) routing
        # client financial data to an arbitrary external address. Comma-
        # separated env override; a blank/unset env falls back to the default.
        self.ALERT_MAIL_ALLOWED_DOMAINS = {
            d.strip().lower().lstrip("@")
            for d in (
                os.environ.get("ALERT_MAIL_ALLOWED_DOMAINS")
                or "kohleservices.com,kcmtrade.com"
            ).split(",")
            if d.strip()
        }

    @property
    def repo_root(self) -> Path:
        # This file: backend/app/core/config.py -> repo root is parents[3]
        return Path(__file__).resolve().parents[3]

    @property
    def parquet_dir(self) -> Path:
        if self.PARQUET_DIR:
            return Path(self.PARQUET_DIR)
        return self.repo_root / "backend" / "data"

    @property
    def public_export_dir(self) -> Path:
        if self.PUBLIC_EXPORT_DIR:
            return Path(self.PUBLIC_EXPORT_DIR)
        return self.repo_root / "frontend" / "public"

    # --- Helpers for services ---
    def postgres_dsn(self) -> str:
        """构建 PostgreSQL DSN。供服务层直接使用。

        fresh grad note: 使用 simple DSN 便于 psycopg2 连接；避免在代码各处手拼接。
        """
        host = self.POSTGRES_HOST or "localhost"
        port = self.POSTGRES_PORT
        db = self.POSTGRES_DBNAME or "reporting_db"
        user = self.POSTGRES_USER or "postgres"
        password = self.POSTGRES_PASSWORD or ""
        return f"host={host} port={port} dbname={db} user={user} password={password}"

    def risk_cases_pg_configured(self) -> bool:
        """True when the OPT-0047 case-layer PG credentials are present."""
        return bool(
            self.POSTGRES_HOST
            and self.RISK_CASES_PG_DBNAME
            and self.RISK_CASES_PG_USER
            and self.RISK_CASES_PG_PASSWORD
        )

    def risk_cases_pg_dsn(self) -> str:
        """DSN for the risk-V2 case database (OPT-0047).

        Host/port reuse the reporting-PG server (same Azure flexible server);
        dbname/user/password are the dedicated least-privilege `risk_cases` /
        `risk_app` pair created 2026-07-12.
        """
        host = self.POSTGRES_HOST or "localhost"
        port = self.POSTGRES_PORT
        db = self.RISK_CASES_PG_DBNAME or "risk_cases"
        user = self.RISK_CASES_PG_USER or "risk_app"
        password = self.RISK_CASES_PG_PASSWORD or ""
        return f"host={host} port={port} dbname={db} user={user} password={password}"


def get_settings() -> Settings:
    return Settings()


