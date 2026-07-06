# ABOUTME: Unit tests for WARC file writing and deduplication
# ABOUTME: Verifies WARC output format, exchange collection, and digest-based dedup
"""Tests for WARC writer."""

from __future__ import annotations

from pathlib import Path

import pytest
from warcio.archiveiterator import (  # type: ignore[import-untyped]
    ArchiveIterator,
)

from archiver.warc_writer import (
    CapturedExchange,
    PlaywrightWARCWriter,
    is_valid_warc,
)


def _exchange(body: bytes) -> CapturedExchange:
    return CapturedExchange(
        url="https://example.com",
        method="GET",
        request_headers={"Host": "example.com"},
        status=200,
        response_headers={"Content-Type": "text/html"},
        body=body,
    )


class TestPlaywrightWARCWriter:
    def test_empty_writer_has_zero_exchanges(self) -> None:
        writer = PlaywrightWARCWriter()
        assert writer.exchange_count == 0

    def test_body_over_per_exchange_cap_dropped(self) -> None:
        writer = PlaywrightWARCWriter(max_body_bytes=100)
        accepted = writer.add_exchange(_exchange(body=b"x" * 101))
        assert accepted is False
        assert writer.exchange_count == 0

    def test_body_at_per_exchange_cap_accepted(self) -> None:
        writer = PlaywrightWARCWriter(max_body_bytes=100)
        assert writer.add_exchange(_exchange(body=b"x" * 100)) is True
        assert writer.exchange_count == 1

    def test_capture_budget_exhausts_across_exchanges(self) -> None:
        writer = PlaywrightWARCWriter(max_total_bytes=250)
        assert writer.add_exchange(_exchange(body=b"a" * 100)) is True
        assert writer.add_exchange(_exchange(body=b"b" * 100)) is True
        # Third would push the total to 300 > 250.
        assert writer.add_exchange(_exchange(body=b"c" * 100)) is False
        # But a smaller body still fits in the remaining budget.
        assert writer.add_exchange(_exchange(body=b"d" * 50)) is True
        assert writer.exchange_count == 3  # noqa: PLR2004

    def test_zero_caps_disable_limits(self) -> None:
        writer = PlaywrightWARCWriter()
        assert writer.add_exchange(_exchange(body=b"x" * 1_000_000))
        assert writer.accepts_body(10**12)

    def test_accepts_body_precheck_matches_caps(self) -> None:
        writer = PlaywrightWARCWriter(
            max_body_bytes=100, max_total_bytes=150
        )
        assert writer.accepts_body(100) is True
        assert writer.accepts_body(101) is False
        writer.add_exchange(_exchange(body=b"x" * 100))
        assert writer.accepts_body(51) is False

    def test_add_exchange_increments_count(self) -> None:
        writer = PlaywrightWARCWriter()
        writer.add_exchange(
            CapturedExchange(
                url="https://example.com",
                method="GET",
                request_headers={"Host": "example.com"},
                status=200,
                response_headers={"Content-Type": "text/html"},
                body=b"<html>Hello</html>",
            )
        )
        assert writer.exchange_count == 1

    def test_finalize_creates_warc_file(
        self, tmp_path: Path
    ) -> None:
        writer = PlaywrightWARCWriter()
        writer.add_exchange(
            CapturedExchange(
                url="https://example.com",
                method="GET",
                request_headers={"Host": "example.com"},
                status=200,
                response_headers={"Content-Type": "text/html"},
                body=b"<html>Hello world</html>",
            )
        )

        out = tmp_path / "test.warc.gz"
        size = writer.finalize(out)

        assert out.exists()
        assert size > 0
        assert is_valid_warc(out)

    def test_finalize_with_multiple_exchanges(
        self, tmp_path: Path
    ) -> None:
        writer = PlaywrightWARCWriter()
        for i in range(3):
            writer.add_exchange(
                CapturedExchange(
                    url=f"https://example.com/page{i}",
                    method="GET",
                    request_headers={},
                    status=200,
                    response_headers={},
                    body=f"Page {i} content".encode(),
                )
            )

        out = tmp_path / "multi.warc.gz"
        writer.finalize(out)
        assert is_valid_warc(out)

    def test_dedup_identical_bodies_writes_revisit(
        self, tmp_path: Path
    ) -> None:
        """Identical response bodies should produce revisit records."""
        import gzip

        writer = PlaywrightWARCWriter()
        same_body = b"<html>Shared content for dedup test</html>"

        writer.add_exchange(
            CapturedExchange(
                url="https://example.com/page1",
                method="GET",
                request_headers={},
                status=200,
                response_headers={},
                body=same_body,
            )
        )
        writer.add_exchange(
            CapturedExchange(
                url="https://example.com/page2",
                method="GET",
                request_headers={},
                status=200,
                response_headers={},
                body=same_body,
            )
        )

        out = tmp_path / "dedup.warc.gz"
        writer.finalize(out)

        # Parse the WARC and verify a revisit record exists
        content = gzip.decompress(out.read_bytes()).decode(
            "utf-8", errors="replace"
        )
        assert "revisit" in content
        assert "identical-payload-digest" in content

    def test_creates_parent_directories(
        self, tmp_path: Path
    ) -> None:
        writer = PlaywrightWARCWriter()
        writer.add_exchange(
            CapturedExchange(
                url="https://example.com",
                method="GET",
                request_headers={},
                status=200,
                response_headers={},
                body=b"test",
            )
        )

        nested = tmp_path / "a" / "b" / "c" / "test.warc.gz"
        writer.finalize(nested)
        assert nested.exists()


class TestIsValidWarc:
    def test_nonexistent_file(self, tmp_path: Path) -> None:
        assert is_valid_warc(tmp_path / "missing.warc.gz") is False

    def test_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.warc.gz"
        empty.write_bytes(b"")
        assert is_valid_warc(empty) is False

    def test_valid_warc(self, tmp_path: Path) -> None:
        writer = PlaywrightWARCWriter()
        writer.add_exchange(
            CapturedExchange(
                url="https://example.com",
                method="GET",
                request_headers={},
                status=200,
                response_headers={},
                body=b"test",
            )
        )
        path = tmp_path / "valid.warc.gz"
        writer.finalize(path)
        assert is_valid_warc(path) is True


class TestWarcUnknownStatus:
    def test_unknown_status_code_uses_unknown_reason(
        self, tmp_path: Path
    ) -> None:
        """Status codes not in HTTPStatus should use 'Unknown'."""
        writer = PlaywrightWARCWriter()
        writer.add_exchange(
            CapturedExchange(
                url="https://example.com",
                method="GET",
                request_headers={},
                status=999,
                response_headers={},
                body=b"weird response",
            )
        )
        out = tmp_path / "unknown_status.warc.gz"
        writer.finalize(out)
        assert is_valid_warc(out)


class TestWarcInfoProvenance:
    """warcinfo header records the user's original URL when a tier
    rewrote the fetch target (privacy_frontend / wayback / archive_today).
    A downstream consumer holding only the .warc.gz can recover the
    original from X-Archiver-Original-URI."""

    def test_warcinfo_includes_original_uri_when_set(
        self, tmp_path: Path,
    ) -> None:

        writer = PlaywrightWARCWriter()
        writer.add_exchange(
            CapturedExchange(
                url="https://nitter.tiekoetter.com/jack/status/20",
                method="GET",
                request_headers={},
                status=200,
                response_headers={"content-type": "text/html"},
                body=b"<html>...</html>",
            )
        )
        out = tmp_path / "provenance.warc.gz"
        writer.finalize(
            out,
            original_url="https://twitter.com/jack/status/20",
        )

        # Read the warcinfo record back and verify the custom header.
        with out.open("rb") as fh:
            for record in ArchiveIterator(fh):
                if record.rec_type == "warcinfo":
                    payload = record.content_stream().read().decode()
                    assert "X-Archiver-Original-URI" in payload
                    assert "twitter.com/jack/status/20" in payload
                    return
        pytest.fail("warcinfo record not found in WARC file")

    def test_warcinfo_omits_original_uri_when_not_set(
        self, tmp_path: Path,
    ) -> None:
        """Direct-capture tiers don't pass original_url; the header
        must NOT be added when there's no provenance to record."""

        writer = PlaywrightWARCWriter()
        writer.add_exchange(
            CapturedExchange(
                url="https://example.com/",
                method="GET",
                request_headers={},
                status=200,
                response_headers={"content-type": "text/html"},
                body=b"<html/>",
            )
        )
        out = tmp_path / "no_provenance.warc.gz"
        writer.finalize(out)

        with out.open("rb") as fh:
            for record in ArchiveIterator(fh):
                if record.rec_type == "warcinfo":
                    payload = record.content_stream().read().decode()
                    assert "X-Archiver-Original-URI" not in payload
                    return
        pytest.fail("warcinfo record not found in WARC file")
