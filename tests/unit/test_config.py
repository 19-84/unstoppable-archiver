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
        assert s.max_capture_timeout == 60  # noqa: PLR2004
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
        assert "postgresql" not in r
        assert "db_url" not in r
