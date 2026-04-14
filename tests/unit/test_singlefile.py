# ABOUTME: Unit tests for SingleFile bundle loading and option building
# ABOUTME: Verifies bundle caching, option construction, and JS template correctness
"""Tests for SingleFile integration."""

from __future__ import annotations

from pathlib import Path

from archiver.singlefile import (
    SINGLEFILE_CAPTURE_JS,
    _load_bundle_cached,
    build_options,
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
