# ABOUTME: Smoke tests verifying the package is importable
# ABOUTME: Validates basic project structure and version availability
"""Smoke tests to verify package is importable and tooling works."""

from __future__ import annotations


def test_import_archiver() -> None:
    import archiver

    assert hasattr(archiver, "__version__")
    assert archiver.__version__ == "0.1.0"
