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

    # Capture — hard ceiling on a single tier attempt. Bounds hangs
    # (Camoufox stuck in an Anubis challenge, SOCKS5 dropped mid-page,
    # SingleFile JS in an infinite loop) so the job escalates to the
    # next tier instead of blocking the worker forever. Must comfortably
    # exceed legitimate captures: real chromium runs on heavy pages
    # (Reddit, Twitter, news sites) hit 60-180 s, plus SingleFile
    # serialization + WARC write add another 30-60 s.
    max_capture_timeout: int = 300
    thumbnail_width: int = 320
    thumbnail_height: int = 240
    chromium_headless: bool = True
    camoufox_headless: bool | str = "virtual"

    # Worker
    worker_id: str = Field(default="worker-1")
    worker_poll_interval: float = 5.0
    max_concurrent_captures: int = 2
    recapture_interval_seconds: int = 3600
    # Prometheus metrics scrape port for the worker process. 0 disables.
    worker_metrics_port: int = 9090

    # Proxy — custom proxies for CAMOUFOX_PROXY tier (tier 3 of 5).
    # `proxy_list` accepts: comma-separated endpoints, or a path to a
    # newline-separated file. Each endpoint is protocol://host:port.
    proxy_list: str = ""
    # `proxy_list_urls` accepts comma-separated URLs to fetch proxy lists
    # from at startup (e.g., curated GitHub-raw proxy list files).
    # Examples: https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt
    # Entries are normalized to scheme://host:port and unioned with proxy_list.
    proxy_list_urls: str = ""
    # Cap on total proxies loaded. 0 = no cap. Lists are usually in the
    # low thousands post-dedup; health-check runs with bounded concurrency
    # so even 10k entries complete in a few minutes. Cap exists only as
    # a circuit-breaker against pathologically large lists.
    proxy_max_count: int = 0
    # Default URL scheme to assume when a proxy entry lacks one (most
    # curated lists emit bare "host:port" and split HTTP/SOCKS by filename).
    proxy_default_scheme: str = "http"
    # Health-check: probe each loaded proxy once at startup and discard
    # those that don't respond in time. Disable for fast iteration on
    # static, already-validated lists.
    proxy_health_check_enabled: bool = True
    proxy_health_check_url: str = "https://httpbin.org/ip"
    proxy_health_check_timeout: float = 8.0
    proxy_health_check_concurrency: int = 20

    # Tor / I2P
    tor_proxy: str = "socks5://tor:9050"
    i2p_proxy: str = "http://i2p:4444"

    # API auth — API key for destructive operations (empty = no auth required)
    api_key: SecretStr = SecretStr("")

    # Admin auth — bcrypt hash, empty disables admin UI entirely
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("change-me-in-production-via-env-var")

    # Abuse prevention — rate limiting (auto-on in public mode unless overridden)
    rate_limit_enabled: bool | None = None
    rate_limit_submit_per_hour: int = 60
    rate_limit_report_per_hour: int = 10

    # Captcha — provider-agnostic. "none" = disabled.
    # "hcaptcha" = third-party visual (needs sitekey + secret from hcaptcha.com)
    # "altcha" = self-hosted proof-of-work (needs hmac_key, no external service)
    captcha_provider: Literal["none", "hcaptcha", "altcha"] = "none"
    hcaptcha_sitekey: str = ""
    hcaptcha_secret: SecretStr = SecretStr("")
    altcha_hmac_key: SecretStr = SecretStr("")
    altcha_max_number: int = 50000  # PoW complexity upper bound

    # Domain blocklist (refuses capture of listed apex domains + all subdomains)
    # Sources are unioned; supports hosts file format (0.0.0.0 example.com) or plain list
    blocklist_file: Path | None = None
    blocklist_urls: str = ""  # comma-separated remote URLs (e.g. GitHub raw lists)
    blocklist_domains: str = ""  # comma-separated inline apex domains

    # Domain allowlist — overrides blocklist; longest match wins
    allowlist_file: Path | None = None
    allowlist_urls: str = ""
    allowlist_domains: str = ""

    # Privacy — IPs are hashed on receipt and never stored raw.
    # Salt for HMAC-SHA256 hash; if empty, falls back to session_secret.
    # Same IP → same hash (for abuse correlation) but hash is non-reversible.
    ip_hash_salt: SecretStr = SecretStr("")
    trusted_proxies: bool = False  # if True, trust X-Forwarded-For header

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    @model_validator(mode="after")
    def _apply_mode_defaults(self) -> Settings:
        """Auto-enable rate limiting in public mode (unless explicitly overridden)."""
        if self.rate_limit_enabled is None:
            self.rate_limit_enabled = self.mode == "public"
        return self

    @property
    def admin_enabled(self) -> bool:
        """True when admin routes are active (password hash configured)."""
        return bool(self.admin_password_hash.get_secret_value())
