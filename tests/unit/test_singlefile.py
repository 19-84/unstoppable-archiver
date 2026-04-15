# ABOUTME: Unit tests for SingleFile bundle loading, CLI subprocess, and option building
# ABOUTME: Verifies bundle caching, CLI capture, timeout handling, and JS template
"""Tests for SingleFile integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archiver.errors import CaptureError
from archiver.singlefile import (
    SINGLEFILE_CAPTURE_JS,
    _load_bundle_cached,
    build_options,
    capture_via_cli,
    cli_available,
    load_bundle,
)


class TestLoadBundle:
    def test_loads_file_content(self, tmp_path: Path) -> None:
        bundle_file = tmp_path / "test-bundle.js"
        bundle_file.write_text("const singlefile = {};")

        _load_bundle_cached.cache_clear()
        result = load_bundle(bundle_file)
        assert result == "const singlefile = {};"
        _load_bundle_cached.cache_clear()

    def test_caches_after_first_load(self, tmp_path: Path) -> None:
        bundle_file = tmp_path / "test-bundle.js"
        bundle_file.write_text("original")

        _load_bundle_cached.cache_clear()
        load_bundle(bundle_file)

        bundle_file.write_text("modified")
        result = load_bundle(bundle_file)
        assert result == "original"  # Still cached
        _load_bundle_cached.cache_clear()

    def test_strips_es_module_wrapper(self, tmp_path: Path) -> None:
        bundle_file = tmp_path / "esm-bundle.js"
        bundle_file.write_text(
            'const script = "var singlefile=1;";export { script };'
        )
        _load_bundle_cached.cache_clear()
        result = load_bundle(bundle_file)
        assert "export" not in result
        assert "(0, eval)(script);" in result
        _load_bundle_cached.cache_clear()


class TestCliAvailable:
    def test_returns_true_when_found(self) -> None:
        with patch("archiver.singlefile.shutil.which", return_value="/usr/bin/single-file"):
            assert cli_available() is True

    def test_returns_false_when_not_found(self) -> None:
        with patch("archiver.singlefile.shutil.which", return_value=None):
            assert cli_available() is False


class TestCaptureViaCli:
    @patch("archiver.singlefile.asyncio.create_subprocess_exec")
    async def test_success(self, mock_exec: MagicMock) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(b"<html>captured</html>", b"")
        )
        proc.returncode = 0
        mock_exec.return_value = proc

        result = await capture_via_cli("https://example.com")
        assert result == "<html>captured</html>"

    @patch("archiver.singlefile.asyncio.create_subprocess_exec")
    async def test_nonzero_exit_raises(self, mock_exec: MagicMock) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(b"", b"Error: page not found")
        )
        proc.returncode = 1
        mock_exec.return_value = proc

        with pytest.raises(CaptureError, match="single-file-cli exit 1"):
            await capture_via_cli("https://example.com")

    @patch("archiver.singlefile.asyncio.create_subprocess_exec")
    async def test_timeout_kills_process(self, mock_exec: MagicMock) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        proc.kill = MagicMock()
        mock_exec.return_value = proc

        with pytest.raises(CaptureError, match="timed out"):
            await capture_via_cli("https://example.com", timeout=1)
        proc.kill.assert_called_once()

    @patch("archiver.singlefile.asyncio.create_subprocess_exec")
    async def test_browser_path_passed(self, mock_exec: MagicMock) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"<html/>", b""))
        proc.returncode = 0
        mock_exec.return_value = proc

        await capture_via_cli(
            "https://example.com",
            browser_path="/usr/bin/chromium",
        )
        cmd = mock_exec.call_args[0]
        assert "--browser-executable-path" in cmd
        assert "/usr/bin/chromium" in cmd


class TestBuildOptions:
    def test_returns_dict_with_url(self) -> None:
        opts = build_options("https://example.com")
        assert opts["url"] == "https://example.com"

    def test_deferred_images_enabled(self) -> None:
        opts = build_options("https://example.com")
        assert opts["loadDeferredImages"] is True

    def test_scripts_removed(self) -> None:
        opts = build_options("https://example.com")
        assert opts["removeScripts"] is True

    def test_html_compression_enabled(self) -> None:
        opts = build_options("https://example.com")
        assert opts["compressHTML"] is True


class TestCaptureJs:
    def test_js_template_is_async_function(self) -> None:
        assert "async" in SINGLEFILE_CAPTURE_JS
        assert "singlefile.getPageData" in SINGLEFILE_CAPTURE_JS
        assert "content" in SINGLEFILE_CAPTURE_JS
