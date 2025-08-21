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

    # Paths (resolved relative to repo root by default)
    PARQUET_DIR: str | None
    PUBLIC_EXPORT_DIR: str | None

    # CORS
    CORS_ORIGINS: List[str]

    def __init__(self) -> None:
        self.DB_HOST = os.environ.get("DB_HOST")
        self.DB_USER = os.environ.get("DB_USER")
        self.DB_PASSWORD = os.environ.get("DB_PASSWORD")
        self.DB_NAME = os.environ.get("DB_NAME")
        self.DB_PORT = int(os.environ.get("DB_PORT", "3306"))
        self.DB_CHARSET = os.environ.get("DB_CHARSET", "utf8mb4")

        self.PARQUET_DIR = os.environ.get("PARQUET_DIR")
        self.PUBLIC_EXPORT_DIR = os.environ.get("PUBLIC_EXPORT_DIR")

        self.CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

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


def get_settings() -> Settings:
    return Settings()


