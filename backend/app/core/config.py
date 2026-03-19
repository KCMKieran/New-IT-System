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

        # API Key for protecting /api/* endpoints (None = skip validation, for dev)
        self.API_KEY = os.environ.get("API_KEY")

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


def get_settings() -> Settings:
    return Settings()


