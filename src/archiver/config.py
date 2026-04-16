# ABOUTME: Application configuration via Pydantic Settings with ARCHIVER_ env prefix
# ABOUTME: Central config consumed by API server, worker, capture engine, and proxy routing
"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables with ARCHIVER_ prefix."""

    model_config = SettingsConfigDict(env_prefix="ARCHIVER_", extra="forbid")

    # Deployment mode — self-hosted (default, single user) or public (archive.today style)
    mode: Literal["self-hosted", "public"] = "self-hosted"

    # Database
    db_url: SecretStr = SecretStr(
        "postgresql://archiver:archiver@localhost:5432/archiver"
    )

    # Storage
    artifacts_dir: Path = Path("data/archives")

    # SingleFile
    singlefile_bundle_path: Path = Path("src/archiver/vendor/single-file-bundle.js")
    singlefile_cli_path: str = "single-file"

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

    # API auth — API key for destructive operations (empty = no auth required)
    api_key: SecretStr = SecretStr("")

    # Admin auth — bcrypt hash, empty disables admin UI entirely
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("change-me-in-production-via-env-var")

    # Abuse prevention — None auto-enables based on mode, explicit bool overrides
    rate_limit_enabled: bool | None = None
    rate_limit_submit_per_hour: int = 60
    rate_limit_report_per_hour: int = 10

    captcha_enabled: bool | None = None
    hcaptcha_sitekey: str = ""
    hcaptcha_secret: SecretStr = SecretStr("")

    # Content policy — None auto-enables based on mode
    respect_robots_txt: bool | None = None
    url_blocklist_patterns: str = ""  # comma-separated regex patterns
    submitter_ip_retention_days: int = 30
    trusted_proxies: bool = False  # if True, trust X-Forwarded-For header

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    @model_validator(mode="after")
    def _apply_mode_defaults(self) -> Settings:
        """Auto-enable public-mode features when mode=public (unless overridden)."""
        public = self.mode == "public"
        if self.rate_limit_enabled is None:
            self.rate_limit_enabled = public
        if self.captcha_enabled is None:
            self.captcha_enabled = public
        if self.respect_robots_txt is None:
            self.respect_robots_txt = public
        return self

    @property
    def admin_enabled(self) -> bool:
        """True when admin routes are active (password hash configured)."""
        return bool(self.admin_password_hash.get_secret_value())
