# ABOUTME: Application configuration via Pydantic Settings with ARCHIVER_ env prefix
# ABOUTME: Central config consumed by API server, worker, capture engine, and proxy routing
"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables with ARCHIVER_ prefix."""

    model_config = SettingsConfigDict(env_prefix="ARCHIVER_", extra="forbid")

    # Database
    db_url: str = "postgresql://archiver:archiver@localhost:5432/archiver"

    # Storage
    artifacts_dir: Path = Path("data/archives")

    # SingleFile
    singlefile_bundle_path: Path = Path("src/archiver/vendor/single-file-bundle.js")

    # Capture
    max_capture_timeout: int = 60
    thumbnail_width: int = 320
    thumbnail_height: int = 240
    chromium_headless: bool = True
    camoufox_headless: bool | str = "virtual"

    # Worker
    worker_id: str = Field(default="worker-1")
    worker_poll_interval: float = 5.0
    max_concurrent_captures: int = 2
    recapture_interval_seconds: int = 3600

    # Proxy
    proxy_list: str = ""  # comma-separated protocol://host:port or path to file

    # Tor / I2P
    tor_proxy: str = "socks5://tor:9050"
    i2p_proxy: str = "http://i2p:4444"

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
