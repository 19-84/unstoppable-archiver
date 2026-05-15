# ABOUTME: Unit tests for Settings configuration and validation
# ABOUTME: Verifies defaults, env prefix, extra=forbid rejection, and repr safety
"""Tests for application configuration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from archiver.config import Settings


class TestSettings:
    def test_defaults(self) -> None:
        s = Settings()
        assert s.log_level == "INFO"
        assert s.log_format == "json"
        assert s.max_capture_timeout == 300  # noqa: PLR2004
        assert s.max_concurrent_captures == 2  # noqa: PLR2004
        assert s.chromium_headless is True
        assert s.artifacts_dir == Path("data/archives")
        assert s.worker_id == "worker-1"
        assert s.proxy_list == ""
        assert s.tor_proxy == "socks5://tor:9050"
        assert s.i2p_proxy == "http://i2p:4444"
        assert s.recapture_interval_seconds == 3600  # noqa: PLR2004

    def test_env_prefix(self) -> None:
        with patch.dict(
            "os.environ", {"ARCHIVER_LOG_LEVEL": "DEBUG"}
        ):
            s = Settings()
            assert s.log_level == "DEBUG"

    def test_extra_forbid_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            Settings(totally_bogus_field="x")  # type: ignore[call-arg]

    def test_db_url_hidden_from_repr(self) -> None:
        s = Settings()
        r = repr(s)
        # SecretStr masks the value in repr
        assert "postgresql://" not in r

    def test_db_url_is_secret_str(self) -> None:
        s = Settings()
        assert "postgresql" in s.db_url.get_secret_value()

    def test_admin_disabled_skips_session_secret_validation(self) -> None:
        """When admin auth is off, the session_secret placeholder is
        fine — sessions are never created and the key is unused."""
        s = Settings()  # admin_password_hash="" by default
        assert s.admin_enabled is False
        # No exception; the placeholder is accepted because the key
        # never signs anything in this mode.
        assert "change-me" in s.session_secret.get_secret_value()

    def test_admin_enabled_rejects_placeholder_secret(self) -> None:
        """Enabling admin with the source-tree placeholder session
        secret must hard-fail at config load — running production
        with a known signing key means anyone with the source can
        forge admin sessions. The error must name the env var so the
        operator knows how to fix it."""
        with pytest.raises(ValidationError, match="ARCHIVER_SESSION_SECRET"):
            Settings(
                admin_password_hash="$2b$12$abcdefghijklmnopqrstuv",  # type: ignore[arg-type] # noqa: S106
            )

    def test_admin_enabled_rejects_short_secret(self) -> None:
        """Even a non-placeholder secret must be long enough to
        resist brute-force on the signing key — 32 chars gives
        ~128 bits of entropy if the value is random."""
        with pytest.raises(ValidationError, match="at least 32"):
            Settings(
                admin_password_hash="$2b$12$abcdefghijklmnopqrstuv",  # type: ignore[arg-type] # noqa: S106
                session_secret="too-short",  # type: ignore[arg-type] # noqa: S106
            )

    def test_captcha_altcha_requires_hmac_key(self) -> None:
        """captcha_provider=altcha without ARCHIVER_ALTCHA_HMAC_KEY
        must fail at boot — without it the challenge endpoint 500s
        the first time a user fetches a captcha. Surface the
        misconfiguration at config load instead."""
        with pytest.raises(ValidationError, match="ARCHIVER_ALTCHA_HMAC_KEY"):
            Settings(captcha_provider="altcha")

    def test_captcha_hcaptcha_requires_sitekey(self) -> None:
        with pytest.raises(ValidationError, match="ARCHIVER_HCAPTCHA_SITEKEY"):
            Settings(captcha_provider="hcaptcha")

    def test_captcha_hcaptcha_requires_secret(self) -> None:
        """Sitekey alone isn't enough — without the server-side
        secret, hcaptcha verification can't happen."""
        with pytest.raises(ValidationError, match="ARCHIVER_HCAPTCHA_SECRET"):
            Settings(
                captcha_provider="hcaptcha",
                hcaptcha_sitekey="10000000-ffff-ffff-ffff-000000000001",
            )

    def test_captcha_none_skips_validation(self) -> None:
        """The default captcha_provider=none must not require any of
        the credential fields — operators who don't run captcha
        shouldn't have to set placeholder keys."""
        s = Settings()
        assert s.captcha_provider == "none"

    def test_admin_enabled_accepts_valid_secret(self) -> None:
        """The happy path — a 32+ char operator-provided secret is
        accepted and the session lifetime defaults to 24h."""
        s = Settings(
            admin_password_hash="$2b$12$abcdefghijklmnopqrstuv",  # type: ignore[arg-type] # noqa: S106
            session_secret="proper-32-byte-random-secret-for-prod-use",  # type: ignore[arg-type] # noqa: S106
        )
        assert s.admin_enabled is True
        assert s.session_lifetime_seconds == 86400  # 24h default  # noqa: PLR2004
