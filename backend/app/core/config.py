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

        # Login IP → CRM last close position IP. Dedicated credentials,
        # isolated from CRM_RISK_API_* so either integration can be revoked
        # without breaking the other.
        self.CRM_LAST_CLOSE_IP_API_URL = (
            os.environ.get("CRM_LAST_CLOSE_IP_API_URL") or ""
        ).strip()
        self.CRM_LAST_CLOSE_IP_API_TOKEN = (
            os.environ.get("CRM_LAST_CLOSE_IP_API_TOKEN") or ""
        ).strip()
        # Master gate for live writes. false = resolve + diff + log only.
        self.LAST_CLOSE_IP_CRM_WRITE_ENABLED = (
            os.environ.get("LAST_CLOSE_IP_CRM_WRITE_ENABLED", "false").strip().lower()
            == "true"
        )
        # Absolute volume ceiling: a run wanting more writes than this aborts
        # having written nothing.
        #
        # Be clear about what this does and does NOT catch (measured 2026-07-15).
        # Daily volume is ~800 writes / ~1,200 clients, and the snapshot itself
        # caps the plausible maximum near 1,300 — so this does NOT fire for any
        # normal-shaped failure, including a totally broken diff re-pushing every
        # client (1,206 < 5,000), nor for the genuinely dangerous case of pushing
        # WRONG values to the right clients. Those are covered elsewhere:
        #   - re-pushing correct values is harmless: write_field reads first and
        #     no-ops when CRM already agrees, so a broken diff costs reads, not writes
        #   - wrong values are caught by nothing automatic — that is the price of
        #     skipping the canary; value_before is the rollback and the first
        #     round's digest is the human check
        # What it IS: a ceiling on an upstream explosion (e.g. someone reverts the
        # analyzer's demo filter or close-only narrowing and the snapshot jumps from
        # 1,488 to five figures). Set well above any plausible day so it never
        # false-trips; it only ever fires on something absurd.
        self.LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN = int(
            os.environ.get("LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN", "5000")
        )
        # Digest recipient. Every run emails here — including aborts, since a
        # push that silently stops looks identical to one with nothing to do.
        # .strip() is load-bearing: backend/.env is CRLF, so a value read through
        # compose's env_file can arrive with a trailing \r that would otherwise
        # ride into the SMTP envelope.
        self.LAST_CLOSE_IP_CRM_MAIL_TO = (
            os.environ.get("LAST_CLOSE_IP_CRM_MAIL_TO")
            or "it.support@kohleservices.com"
        ).strip()

        # --- Geo enrichment for the last-close-IP push (2026-07-17) -----------
        # The pushed value is "1.2.3.4 (CN)"; the country comes from MaxMind's
        # GeoIP2 Precision Country web service. Same vendor account as
        # Manager_Log_Monitor, which is why these names match its config.
        #
        # .strip() is load-bearing here for the same CRLF reason as MAIL_TO above,
        # and it bites harder: a trailing \r on the license key is invisible in
        # logs and fails auth as a flat 401, which reads like "wrong key".
        self.MAXMIND_ACCOUNT_ID = (os.environ.get("MAXMIND_ACCOUNT_ID") or "").strip()
        self.MAXMIND_LICENSE_KEY = (os.environ.get("MAXMIND_LICENSE_KEY") or "").strip()
        self.MAXMIND_HOST = (
            os.environ.get("MAXMIND_HOST") or "geoip.maxmind.com"
        ).strip()
        self.MAXMIND_TIMEOUT = float(os.environ.get("MAXMIND_TIMEOUT", "10"))
        # Parallelism for the per-IP lookups. ~1,190 distinct IPs/day at ~150ms
        # each would add ~3min serially; 8 workers brings it under 30s. MaxMind
        # permits concurrent queries — this is polite, not a documented ceiling.
        self.LAST_CLOSE_IP_GEO_WORKERS = int(
            os.environ.get("LAST_CLOSE_IP_GEO_WORKERS", "8")
        )
        # Cache TTL. Exists to bound credit burn, not for speed — see the
        # ip_geo_cache schema comment in login_ip_db.py.
        self.LAST_CLOSE_IP_GEO_CACHE_TTL_DAYS = int(
            os.environ.get("LAST_CLOSE_IP_GEO_CACHE_TTL_DAYS", "30")
        )
        # Systemic-geo-failure gate. If more than this fraction of the day's
        # distinct IPs fail to resolve, the run aborts writing nothing rather
        # than pushing a partial set: that many failures means MaxMind is
        # degraded, not that those IPs are odd.
        self.LAST_CLOSE_IP_GEO_FAIL_ABORT_RATIO = float(
            os.environ.get("LAST_CLOSE_IP_GEO_FAIL_ABORT_RATIO", "0.2")
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
                or "kohleservices.com,kcmtrade.com,th.kohlecapital.com"
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
        # Hardening (OPT-0055):
        # - TCP keepalives detect Azure silently killing idle WAN
        #   connections (pooled connections would otherwise hang on a dead
        #   socket instead of failing fast).
        # - sslmode=require: Azure PG already enforces TLS, this just makes
        #   the requirement explicit client-side (no-op hardening).
        # - statement_timeout=30s: deliberately NOT lower — the case-engine
        #   write pipeline shares this DSN and its DELETE/upserts can wait
        #   on row locks; 30s converts a rare indefinite hang into a
        #   fail-open retry without cancelling legitimately slow writes.
        return (
            f"host={host} port={port} dbname={db} user={user} password={password}"
            " keepalives=1 keepalives_idle=30 keepalives_interval=10"
            " keepalives_count=3 sslmode=require application_name=risk_cases"
            " options='-c statement_timeout=30000'"
        )


def get_settings() -> Settings:
    return Settings()


