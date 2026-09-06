"""Typed configuration — single source of truth, loaded from .env / environment.

Rule (§9 of the strategy spec): no hardcoded paths, ports, or parameters
anywhere in source. Everything flows through this module.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Massive / flat files
    massive_api_key: str = ""
    massive_s3_access_key: str = ""
    massive_s3_secret_key: str = ""
    massive_s3_endpoint: str = "https://files.polygon.io"
    massive_s3_bucket: str = "flatfiles"

    # Storage
    gex_data_root: Path = Path("./data")
    gex_raw_dir: Path | None = None
    gex_parquet_dir: Path | None = None
    gex_manifest_db: Path | None = None

    # Universe
    gex_underlyings: str = "SPX,SPXW,XSP,SPY"
    gex_index_tickers: str = "I:SPX,I:VIX,I:VIX9D,I:VVIX"

    # History window
    gex_start_date: dt.date = dt.date(2021, 1, 4)
    gex_end_date: dt.date | None = None

    # Ports (non-standard by design; this Mac runs other dev work)
    gex_dashboard_port: int = 8741
    gex_api_port: int = 8742

    @field_validator("gex_end_date", mode="before")
    @classmethod
    def _empty_end_date(cls, v: object) -> object:
        return None if v in ("", None) else v

    def model_post_init(self, __context: object) -> None:
        if self.gex_raw_dir is None:
            self.gex_raw_dir = self.gex_data_root / "raw"
        if self.gex_parquet_dir is None:
            self.gex_parquet_dir = self.gex_data_root / "parquet"
        if self.gex_manifest_db is None:
            self.gex_manifest_db = self.gex_data_root / "manifest.duckdb"

    # ── derived helpers ──────────────────────────────────────────────
    @property
    def underlyings(self) -> list[str]:
        return [u.strip().upper() for u in self.gex_underlyings.split(",") if u.strip()]

    @property
    def index_tickers(self) -> list[str]:
        return [t.strip() for t in self.gex_index_tickers.split(",") if t.strip()]

    @property
    def end_date(self) -> dt.date:
        return self.gex_end_date or (dt.date.today() - dt.timedelta(days=1))

    def ensure_dirs(self) -> None:
        for p in (self.gex_raw_dir, self.gex_parquet_dir):
            assert p is not None
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
