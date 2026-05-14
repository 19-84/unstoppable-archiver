# ABOUTME: Unit tests for Common Crawl CDX lookup + range-fetch
# ABOUTME: Mocks CC's index + data endpoints via respx
"""Tests for archiver.commoncrawl."""

from __future__ import annotations

import gzip
import io
import json

import httpx
import pytest
import respx

from archiver import commoncrawl


@pytest.fixture(autouse=True)
def _reset_collinfo_cache() -> object:  # type: ignore[misc]
    """Each test gets a clean collinfo cache."""
    orig = commoncrawl._collinfo_cache
    commoncrawl._collinfo_cache = None
    yield
    commoncrawl._collinfo_cache = orig


def _mock_collinfo(ids: list[str]) -> None:
    """Mock the collinfo.json endpoint."""
    respx.get(commoncrawl._COLLINFO_URL).mock(
        return_value=httpx.Response(
            200, json=[{"id": i, "name": i} for i in ids]
        )
    )


def _cdx_response(url: str, crawl_id: str, status: int = 200) -> dict[str, object]:
    """Build a CDX-shaped record for mocking."""
    return {
        "urlkey": "com,example)/",
        "timestamp": "20260101120000",
        "url": url,
        "mime": "text/html",
        "status": str(status),
        "digest": "X" * 32,
        "length": "1024",
        "offset": "12345",
        "filename": f"crawl-data/{crawl_id}/segments/1/warc/file.warc.gz",
    }


def _build_warc_gz(body: bytes, target_uri: str = "https://example.com/") -> bytes:
    """Construct a minimal WARC response record compressed with gzip."""
    content_len = len(body)
    http_headers = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/html\r\n"
        b"Content-Length: " + str(content_len).encode() + b"\r\n"
        b"\r\n"
    )
    block = http_headers + body
    warc_headers = (
        "WARC/1.0\r\n"
        "WARC-Type: response\r\n"
        f"WARC-Target-URI: {target_uri}\r\n"
        "WARC-Date: 2026-01-01T12:00:00Z\r\n"
        "Content-Type: application/http; msgtype=response\r\n"
        f"Content-Length: {len(block)}\r\n"
        "\r\n"
    ).encode()
    record_uncompressed = warc_headers + block + b"\r\n\r\n"
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(record_uncompressed)
    return buf.getvalue()


class TestListCrawls:
    @respx.mock
    async def test_returns_ids_in_order(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12", "CC-MAIN-2026-08"])
        ids = await commoncrawl.list_crawls()
        assert ids == ["CC-MAIN-2026-12", "CC-MAIN-2026-08"]

    @respx.mock
    async def test_caches_result(self) -> None:
        call_count = {"n": 0}

        def responder(_: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-12"}])

        respx.get(commoncrawl._COLLINFO_URL).mock(side_effect=responder)
        await commoncrawl.list_crawls()
        await commoncrawl.list_crawls()
        assert call_count["n"] == 1


class TestFindSnapshot:
    @respx.mock
    async def test_newest_crawl_hit(self) -> None:
        """First crawl returns a 200; should stop there."""
        _mock_collinfo(["CC-MAIN-2026-12", "CC-MAIN-2026-08", "CC-MAIN-2026-04"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(
            200,
            text=json.dumps(_cdx_response("https://example.com/", "CC-MAIN-2026-12")),
        ))
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-08-index"
        ).mock(return_value=httpx.Response(200, text="No Captures found for: ..."))
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-04-index"
        ).mock(return_value=httpx.Response(200, text="No Captures found for: ..."))

        snap = await commoncrawl.find_snapshot("https://example.com/")
        assert snap is not None
        assert snap.crawl_id == "CC-MAIN-2026-12"
        assert snap.timestamp == "20260101120000"

    @respx.mock
    async def test_all_crawls_miss_returns_none(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12", "CC-MAIN-2026-08", "CC-MAIN-2026-04"])
        for crawl in ("CC-MAIN-2026-12", "CC-MAIN-2026-08", "CC-MAIN-2026-04"):
            respx.get(
                f"https://index.commoncrawl.org/{crawl}-index"
            ).mock(return_value=httpx.Response(
                200, text="No Captures found for: ..."
            ))
        snap = await commoncrawl.find_snapshot("https://example.com/")
        assert snap is None

    @respx.mock
    async def test_rate_limit_treated_as_miss(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(429))
        snap = await commoncrawl.find_snapshot("https://example.com/")
        assert snap is None

    @respx.mock
    async def test_non_200_status_ignored(self) -> None:
        """A CDX record with status!=200 (like a 404 capture) is not
        considered a hit — we want actual content, not captured errors."""
        _mock_collinfo(["CC-MAIN-2026-12"])
        rec = _cdx_response("https://example.com/", "CC-MAIN-2026-12", status=404)
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(200, text=json.dumps(rec)))
        snap = await commoncrawl.find_snapshot("https://example.com/")
        assert snap is None


class TestFetchRecord:
    @respx.mock
    async def test_fetches_and_parses_html(self) -> None:
        warc_bytes = _build_warc_gz(b"<html><body>hello from CC</body></html>")
        snap = commoncrawl.CCSnapshot(
            url="https://example.com/",
            timestamp="20260101120000",
            crawl_id="CC-MAIN-2026-12",
            filename="crawl-data/CC-MAIN-2026-12/foo.warc.gz",
            offset=0,
            length=len(warc_bytes),
            status=200,
            mime="text/html",
        )
        respx.get(snap.fetch_url()).mock(
            return_value=httpx.Response(
                206, content=warc_bytes,
                headers={"Content-Range": f"bytes 0-{len(warc_bytes)-1}"}
            )
        )
        html = await commoncrawl.fetch_record_html(snap)
        assert b"hello from CC" in html

    @respx.mock
    async def test_bad_status_raises(self) -> None:
        snap = commoncrawl.CCSnapshot(
            url="https://example.com/",
            timestamp="20260101120000",
            crawl_id="CC-MAIN-2026-12",
            filename="crawl-data/CC-MAIN-2026-12/foo.warc.gz",
            offset=0, length=100, status=200, mime="text/html",
        )
        respx.get(snap.fetch_url()).mock(
            return_value=httpx.Response(500)
        )
        with pytest.raises(RuntimeError, match="500"):
            await commoncrawl.fetch_record_html(snap)


class TestListCrawlsCache:
    @respx.mock
    async def test_stale_cache_refetches(self) -> None:
        """Cache older than TTL is discarded and CC is re-hit."""
        call_count = {"n": 0}

        def responder(_: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-12"}])

        respx.get(commoncrawl._COLLINFO_URL).mock(side_effect=responder)
        commoncrawl._collinfo_cache = (0.0, ["CC-MAIN-STALE"])
        ids = await commoncrawl.list_crawls()
        assert ids == ["CC-MAIN-2026-12"]
        assert call_count["n"] == 1


class TestQueryCrawlErrors:
    @respx.mock
    async def test_timeout_returns_none(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(side_effect=httpx.TimeoutException("timed out"))
        assert await commoncrawl.find_snapshot("https://example.com/") is None

    @respx.mock
    async def test_unexpected_exception_swallowed(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(side_effect=httpx.ConnectError("boom"))
        assert await commoncrawl.find_snapshot("https://example.com/") is None

    @respx.mock
    async def test_non_200_cdx_returns_none(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(500))
        assert await commoncrawl.find_snapshot("https://example.com/") is None

    @respx.mock
    async def test_invalid_json_body_returns_none(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(200, text="this is not {json"))
        assert await commoncrawl.find_snapshot("https://example.com/") is None

    @respx.mock
    async def test_malformed_record_missing_keys(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(
            200, text=json.dumps({"url": "https://example.com/"})
        ))
        assert await commoncrawl.find_snapshot("https://example.com/") is None

    @respx.mock
    async def test_empty_crawl_list_returns_none(self) -> None:
        _mock_collinfo([])
        assert await commoncrawl.find_snapshot("https://example.com/") is None


class TestFindSnapshotFullHistory:
    @respx.mock
    async def test_finds_hit_in_older_crawl(self) -> None:
        """Full-history scan finds a hit even when recent crawls miss."""
        _mock_collinfo(["CC-MAIN-2026-12", "CC-MAIN-2018-22"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(200, text="No Captures found for: ..."))
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2018-22-index"
        ).mock(return_value=httpx.Response(
            200,
            text=json.dumps(_cdx_response("https://example.com/", "CC-MAIN-2018-22")),
        ))
        snap = await commoncrawl.find_snapshot_full_history("https://example.com/")
        assert snap is not None
        assert snap.crawl_id == "CC-MAIN-2018-22"

    @respx.mock
    async def test_all_miss_returns_none(self) -> None:
        _mock_collinfo(["CC-MAIN-2026-12", "CC-MAIN-2018-22"])
        for crawl in ("CC-MAIN-2026-12", "CC-MAIN-2018-22"):
            respx.get(
                f"https://index.commoncrawl.org/{crawl}-index"
            ).mock(return_value=httpx.Response(200, text="No Captures found for: ..."))
        snap = await commoncrawl.find_snapshot_full_history("https://example.com/")
        assert snap is None

    @respx.mock
    async def test_max_crawls_limits_scan(self) -> None:
        """max_crawls truncates the crawl list before scanning."""
        _mock_collinfo(["CC-MAIN-2026-12", "CC-MAIN-2018-22"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(200, text="No Captures found for: ..."))
        # Second crawl deliberately not mocked — if scan reaches it, respx raises.
        snap = await commoncrawl.find_snapshot_full_history(
            "https://example.com/", max_crawls=1
        )
        assert snap is None

    @respx.mock
    async def test_continues_after_first_hit_when_not_stopping(self) -> None:
        """stop_on_first=False scans all crawls but still returns first hit."""
        _mock_collinfo(["CC-MAIN-2026-12", "CC-MAIN-2018-22"])
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2026-12-index"
        ).mock(return_value=httpx.Response(
            200,
            text=json.dumps(_cdx_response("https://example.com/", "CC-MAIN-2026-12")),
        ))
        respx.get(
            "https://index.commoncrawl.org/CC-MAIN-2018-22-index"
        ).mock(return_value=httpx.Response(
            200,
            text=json.dumps(_cdx_response("https://example.com/", "CC-MAIN-2018-22")),
        ))
        snap = await commoncrawl.find_snapshot_full_history(
            "https://example.com/", stop_on_first=False
        )
        assert snap is not None
        assert snap.crawl_id == "CC-MAIN-2026-12"


class TestFetchRecordNoResponse:
    @respx.mock
    async def test_warc_with_no_response_record_raises(self) -> None:
        """A WARC chunk containing only non-response records must raise."""
        buf = io.BytesIO()
        warc_headers = (
            b"WARC/1.0\r\n"
            b"WARC-Type: warcinfo\r\n"
            b"WARC-Date: 2026-01-01T12:00:00Z\r\n"
            b"Content-Type: application/warc-fields\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n\r\n\r\n"
        )
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(warc_headers)
        warc_bytes = buf.getvalue()

        snap = commoncrawl.CCSnapshot(
            url="https://example.com/",
            timestamp="20260101120000",
            crawl_id="CC-MAIN-2026-12",
            filename="crawl-data/CC-MAIN-2026-12/foo.warc.gz",
            offset=0, length=len(warc_bytes), status=200, mime="text/html",
        )
        respx.get(snap.fetch_url()).mock(
            return_value=httpx.Response(
                206, content=warc_bytes,
                headers={"Content-Range": f"bytes 0-{len(warc_bytes)-1}"}
            )
        )
        with pytest.raises(RuntimeError, match="No response record"):
            await commoncrawl.fetch_record_html(snap)


