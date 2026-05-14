# ABOUTME: Tests for cookie-banner consent injection script builder
# ABOUTME: Verifies CSS/JSON asset loading, script generation, and selector encoding
"""Tests for consent injector."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from archiver import consent


def _reset_caches() -> None:
    consent._load_css.cache_clear()  # pyright: ignore [reportAttributeAccessIssue]
    consent._load_domain_rules.cache_clear()  # pyright: ignore [reportAttributeAccessIssue]


class TestLoadAssets:
    def test_load_css_returns_bundled_content(self) -> None:
        _reset_caches()
        css = consent._load_css()
        # Vendored file exists and is non-empty post-build.
        assert "display: none" in css

    def test_load_domain_rules_has_structure(self) -> None:
        _reset_caches()
        payload = consent._load_domain_rules()
        assert "rules" in payload
        assert "exceptions" in payload
        assert isinstance(payload["rules"], dict)


class TestBuildScript:
    def test_contains_css_blob(self) -> None:
        _reset_caches()
        script = consent.build_consent_init_script()
        assert "display: none" in script
        # IIFE wrapper is present.
        assert script.lstrip().startswith("(()")

    def test_contains_domain_rules_json(self) -> None:
        _reset_caches()
        script = consent.build_consent_init_script()
        # The placeholder was substituted — rules object is embedded.
        assert "DOMAIN_RULES" in script
        assert "EXCEPTIONS" in script

    def test_empty_assets_yields_comment(self, tmp_path: Path) -> None:
        """When vendor files are missing, return a harmless comment."""
        _reset_caches()
        with patch.object(consent, "_CSS_PATH", tmp_path / "missing.css"), \
             patch.object(consent, "_JSON_PATH", tmp_path / "missing.json"):
            _reset_caches()
            script = consent.build_consent_init_script()
            assert script.strip().startswith("/*")
        _reset_caches()

    def test_selectors_with_quotes_survive_encoding(
        self, tmp_path: Path
    ) -> None:
        """Selectors containing quotes / backslashes must JSON-encode safely."""
        _reset_caches()
        css_file = tmp_path / "filters.css"
        css_file.write_text(
            '[data-consent="banner"] { display: none !important; }'
        )
        json_file = tmp_path / "filters.json"
        json_file.write_text(json.dumps({
            "rules": {"example.com": ['[data-x="y"]']},
            "exceptions": {},
        }))
        with patch.object(consent, "_CSS_PATH", css_file), \
             patch.object(consent, "_JSON_PATH", json_file):
            _reset_caches()
            script = consent.build_consent_init_script()
            # Verify the selector round-trips through JSON safely —
            # must not break the host script.
            assert '[data-consent=' in script
            assert '[data-x=' in script
            # Sanity: braces balance (template substitution didn't leak).
            assert script.count("{") == script.count("}")
        _reset_caches()
