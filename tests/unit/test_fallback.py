# ABOUTME: Unit tests for archive fallback — availability checks and browser capture
# ABOUTME: Tests Wayback Machine and archive.today API checks, toolbar stripping, error paths
"""Tests for fallback capture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import respx
from playwright.async_api import Page

from archiver.fallback import (
    ARCHIVE_TODAY_MIRRORS,
    ARCHIVE_TODAY_STRIP_SELECTORS,
    WAYBACK_STRIP_SELECTORS,
    _extract_angle_url,
    _extract_attr,
    _is_archive_today_snapshot_url,
    _wayback_url_variants,
    check_wayback_availability,
    extract_title_from_html,
    fetch_archive_today_snapshot_html,
    find_archive_today_snapshot,
    save_to_archive_today,
    save_to_wayback,
    strip_html_tags,
)


def _mock_page() -> MagicMock:
    """Page mock that passes beartype isinstance checks.

    MagicMock(spec=Page) satisfies isinstance(obj, Page); async methods
    are attached as AsyncMock so await works.
    """
    page = MagicMock(spec=Page)
    # Default successful 200 response from goto.
    ok_response = MagicMock()
    ok_response.status = 200
    page.goto = AsyncMock(return_value=ok_response)
    page.fill = AsyncMock()
    page.click = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.title = AsyncMock(return_value="")
    page.evaluate = AsyncMock(return_value="")
    # locator('...').first.wait_for is used by save_to_archive_today.
    locator_chain = MagicMock()
    locator_chain.first = MagicMock()
    locator_chain.first.wait_for = AsyncMock()
    page.locator = MagicMock(return_value=locator_chain)
    return page


class TestFallbackConstants:
    def test_wayback_strip_selectors_not_empty(self) -> None:
        assert len(WAYBACK_STRIP_SELECTORS) > 0

    def test_archive_today_strip_selectors_not_empty(
        self,
    ) -> None:
        assert len(ARCHIVE_TODAY_STRIP_SELECTORS) > 0

    def test_wayback_strips_toolbar(self) -> None:
        assert "#wm-ib-bar" in WAYBACK_STRIP_SELECTORS

    def test_archive_today_strips_header(self) -> None:
        assert "#HEADER" in ARCHIVE_TODAY_STRIP_SELECTORS


class TestWaybackAvailability:
    @respx.mock
    async def test_available(self) -> None:
        respx.get("https://archive.org/wayback/available").mock(
            return_value=httpx.Response(
                200,
                json={
                    "archived_snapshots": {
                        "closest": {
                            "available": True,
                            "url": "https://web.archive.org/web/2024/https://example.com",
                        }
                    }
                },
            )
        )
        result = await check_wayback_availability("https://example.com")
        assert result is not None
        assert "web.archive.org" in result

    @respx.mock
    async def test_not_available(self) -> None:
        respx.get("https://archive.org/wayback/available").mock(
            return_value=httpx.Response(
                200, json={"archived_snapshots": {}}
            )
        )
        result = await check_wayback_availability("https://example.com")
        assert result is None

    @respx.mock
    async def test_api_error(self) -> None:
        respx.get("https://archive.org/wayback/available").mock(
            return_value=httpx.Response(500)
        )
        result = await check_wayback_availability("https://example.com")
        assert result is None

    @respx.mock
    async def test_request_exception_tries_next_variant(self) -> None:
        """A transport error on variant #1 must not abort — variant #2 used."""
        # First variant errors, subsequent ones return empty.
        call_count = {"n": 0}

        def responder(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"archived_snapshots": {}})

        respx.get("https://archive.org/wayback/available").mock(
            side_effect=responder
        )
        result = await check_wayback_availability("https://example.com")
        assert result is None
        assert call_count["n"] > 1

    @respx.mock
    async def test_malformed_json_tries_next_variant(self) -> None:
        respx.get("https://archive.org/wayback/available").mock(
            return_value=httpx.Response(200, text="not json")
        )
        result = await check_wayback_availability("https://example.com")
        assert result is None


def _mock_all_mirrors(response: httpx.Response) -> None:
    """Mock every archive.today mirror's timemap with the same response."""
    for host in ARCHIVE_TODAY_MIRRORS:
        respx.get(f"https://{host}/timemap/https://example.com").mock(
            return_value=response
        )


class TestMirrorRotation:
    @respx.mock
    async def test_finds_newest_memento(self) -> None:
        """find_archive_today_snapshot returns the latest-datetime memento."""
        body = (
            '<https://example.com>; rel="original",\n'
            '<https://archive.today/2022/foo>; rel="memento";'
            ' datetime="Sat, 01 Jan 2022 00:00:00 GMT",\n'
            '<https://archive.today/2024/foo>; rel="memento";'
            ' datetime="Mon, 01 Jan 2024 00:00:00 GMT",\n'
            '<https://archive.today/2023/foo>; rel="memento";'
            ' datetime="Sun, 01 Jan 2023 00:00:00 GMT"\n'
        )
        _mock_all_mirrors(httpx.Response(200, text=body))
        result = await find_archive_today_snapshot("https://example.com")
        assert result == "https://archive.today/2024/foo"

    @respx.mock
    async def test_succeeds_when_only_backup_mirror_responds(self) -> None:
        """Primary mirror 429s, a backup mirror returns a snapshot."""
        # Primary returns rate-limit
        respx.get(
            "https://archive.today/timemap/https://example.com"
        ).mock(return_value=httpx.Response(429))
        # One backup has the snapshot
        respx.get(
            "https://archive.ph/timemap/https://example.com"
        ).mock(return_value=httpx.Response(
            200,
            text=(
                '<https://archive.ph/2024/foo>; rel="memento";'
                ' datetime="Mon, 01 Jan 2024 00:00:00 GMT"\n'
            ),
        ))
        # Remaining mirrors fail
        for host in ARCHIVE_TODAY_MIRRORS:
            if host in ("archive.today", "archive.ph"):
                continue
            respx.get(f"https://{host}/timemap/https://example.com").mock(
                return_value=httpx.Response(429)
            )
        result = await find_archive_today_snapshot("https://example.com")
        assert result == "https://archive.ph/2024/foo"

    @respx.mock
    async def test_all_mirrors_dead_returns_none(self) -> None:
        _mock_all_mirrors(httpx.Response(503))
        result = await find_archive_today_snapshot("https://example.com")
        assert result is None


class TestTimemapParser:
    def test_extracts_angle_url(self) -> None:
        assert (
            _extract_angle_url('<https://archive.today/2024/x>; rel="memento"')
            == "https://archive.today/2024/x"
        )

    def test_returns_none_when_no_brackets(self) -> None:
        assert _extract_angle_url('no brackets here') is None

    def test_extract_attr(self) -> None:
        assert (
            _extract_attr('<url>; rel="memento"; datetime="Mon, 01 Jan"',
                          "datetime")
            == "Mon, 01 Jan"
        )

    def test_extract_attr_missing(self) -> None:
        assert _extract_attr('<url>; rel="memento"', "datetime") == ""


class TestFetchDirect:
    @respx.mock
    async def test_returns_html_on_200(self) -> None:
        respx.get("https://archive.today/2024/foo").mock(
            return_value=httpx.Response(
                200,
                text="<html><title>My Page</title><body>content</body></html>",
            )
        )
        result = await fetch_archive_today_snapshot_html(
            "https://archive.today/2024/foo"
        )
        assert result is not None
        assert "My Page" in result

    @respx.mock
    async def test_cf_challenge_detected(self) -> None:
        """CF interstitial served with 200 status should be rejected."""
        # All rotation attempts hit the same CF challenge.
        for host in ARCHIVE_TODAY_MIRRORS:
            respx.get(f"https://{host}/2024/foo").mock(
                return_value=httpx.Response(
                    200,
                    text="<html><title>Just a moment...</title></html>",
                )
            )
        result = await fetch_archive_today_snapshot_html(
            "https://archive.today/2024/foo"
        )
        assert result is None

    @respx.mock
    async def test_403_on_all_mirrors_returns_none(self) -> None:
        for host in ARCHIVE_TODAY_MIRRORS:
            respx.get(f"https://{host}/2024/foo").mock(
                return_value=httpx.Response(403)
            )
        result = await fetch_archive_today_snapshot_html(
            "https://archive.today/2024/foo"
        )
        assert result is None

    @respx.mock
    async def test_rotates_to_next_mirror_on_429(self) -> None:
        """Primary mirror rate-limits us; mirror rotation tries the next."""
        # Primary hit rate-limit
        respx.get("https://archive.today/2024/foo").mock(
            return_value=httpx.Response(429)
        )
        # archive.ph serves the same snapshot
        respx.get("https://archive.ph/2024/foo").mock(
            return_value=httpx.Response(
                200,
                text="<html><title>Good</title>rescued via mirror</html>",
            )
        )
        # Remaining mirrors also 429 — shouldn't matter, we should return early.
        for host in ARCHIVE_TODAY_MIRRORS:
            if host in ("archive.today", "archive.ph"):
                continue
            respx.get(f"https://{host}/2024/foo").mock(
                return_value=httpx.Response(429)
            )
        result = await fetch_archive_today_snapshot_html(
            "https://archive.today/2024/foo"
        )
        assert result is not None
        assert "rescued via mirror" in result

    @respx.mock
    async def test_network_error_rotates(self) -> None:
        """ConnectError on primary triggers rotation to next mirror."""
        respx.get("https://archive.today/2024/foo").mock(
            side_effect=httpx.ConnectError("boom")
        )
        respx.get("https://archive.ph/2024/foo").mock(
            return_value=httpx.Response(
                200, text="<html><title>ok</title></html>"
            )
        )
        for host in ARCHIVE_TODAY_MIRRORS:
            if host in ("archive.today", "archive.ph"):
                continue
            respx.get(f"https://{host}/2024/foo").mock(
                return_value=httpx.Response(429)
            )
        result = await fetch_archive_today_snapshot_html(
            "https://archive.today/2024/foo"
        )
        assert result is not None
        assert "ok" in result

    @respx.mock
    async def test_unknown_host_not_rotated(self) -> None:
        """If the URL host isn't a known mirror, don't try to rewrite it."""
        respx.get("https://totally-different-host.example/2024/foo").mock(
            return_value=httpx.Response(429)
        )
        result = await fetch_archive_today_snapshot_html(
            "https://totally-different-host.example/2024/foo"
        )
        assert result is None


class TestHtmlHelpers:
    def test_extract_title(self) -> None:
        assert (
            extract_title_from_html(
                "<html><head><title>Hello</title></head></html>"
            )
            == "Hello"
        )

    def test_extract_title_missing(self) -> None:
        assert extract_title_from_html("<html></html>") == ""

    def test_extract_title_truncates(self) -> None:
        long = "<title>" + "x" * 1000 + "</title>"
        assert len(extract_title_from_html(long)) == 500  # noqa: PLR2004

    def test_strip_html_tags_removes_scripts(self) -> None:
        text = strip_html_tags(
            "<html><script>alert('x')</script><p>Hi</p></html>"
        )
        assert "alert" not in text
        assert "Hi" in text

    def test_strip_html_tags_removes_sloppy_end_tags(self) -> None:
        # Browsers treat </script > (trailing space/attrs) as a
        # closing tag; the stripper must too or the script body
        # lands in the search index.
        text = strip_html_tags(
            "<html><script>alert('x')</script ><p>Hi</p></html>"
        )
        assert "alert" not in text
        assert "Hi" in text

    def test_strip_html_tags_removes_styles(self) -> None:
        text = strip_html_tags(
            "<html><style>body{color:red}</style><p>Hi</p></html>"
        )
        assert "color:red" not in text
        assert "Hi" in text


class TestWaybackUrlVariants:
    def test_adds_trailing_slash_variant(self) -> None:
        variants = _wayback_url_variants("https://example.com/page")
        assert "https://example.com/page" in variants
        assert "https://example.com/page/" in variants

    def test_removes_trailing_slash_variant(self) -> None:
        variants = _wayback_url_variants("https://example.com/page/")
        assert "https://example.com/page" in variants

    def test_toggles_www_prefix(self) -> None:
        variants = _wayback_url_variants("https://example.com/")
        hosts = {u for u in variants if "www.example.com" in u}
        assert hosts  # at least one variant with www

    def test_removes_www_prefix(self) -> None:
        variants = _wayback_url_variants("https://www.example.com/")
        assert any(
            "://example.com" in v and "://www.example.com" not in v
            for v in variants
        )

    def test_deduplicates(self) -> None:
        variants = _wayback_url_variants("https://example.com/")
        assert len(variants) == len(set(variants))

    def test_invalid_url_returns_original(self) -> None:
        variants = _wayback_url_variants("not a url at all")
        assert variants == ["not a url at all"]


class TestWaybackAvailabilityWithVariants:
    @respx.mock
    async def test_second_variant_succeeds(self) -> None:
        # Original returns no snapshot; trailing-slash variant does.
        respx.get("https://archive.org/wayback/available").mock(
            side_effect=[
                httpx.Response(200, json={"archived_snapshots": {}}),
                httpx.Response(
                    200,
                    json={
                        "archived_snapshots": {
                            "closest": {
                                "available": True,
                                "url": "https://web.archive.org/web/2024/https://example.com/",
                            }
                        }
                    },
                ),
            ]
        )
        result = await check_wayback_availability("https://example.com")
        assert result is not None
        assert "web.archive.org" in result


class TestSaveToWayback:
    async def test_success_returns_snapshot_url(self) -> None:
        page = _mock_page()
        page.url = (
            "https://web.archive.org/web/20260417000000/https://example.com/"
        )
        result = await save_to_wayback("https://example.com/", page)
        assert result is not None
        assert "/web/" in result

    async def test_goto_failure_returns_none(self) -> None:
        page = _mock_page()
        page.goto = AsyncMock(side_effect=RuntimeError("boom"))
        result = await save_to_wayback("https://example.com/", page)
        assert result is None

    async def test_spn_http_error_returns_none(self) -> None:
        """SPN endpoint returning 4xx should fail-fast instead of waiting."""
        page = _mock_page()
        err_response = MagicMock()
        err_response.status = 429  # rate-limited
        page.goto = AsyncMock(return_value=err_response)
        page.url = "https://web.archive.org/save/https://example.com/"
        result = await save_to_wayback("https://example.com/", page)
        assert result is None

    async def test_wait_timeout_returns_none(self) -> None:
        page = _mock_page()
        page.url = "https://web.archive.org/save/https://example.com/"
        page.wait_for_function = AsyncMock(
            side_effect=TimeoutError("timed out")
        )
        result = await save_to_wayback("https://example.com/", page)
        assert result is None


class TestArchiveTodaySnapshotUrlPredicate:
    def test_real_snapshot_url(self) -> None:
        assert _is_archive_today_snapshot_url(
            "https://archive.today/abc12/https://example.com/"
        ) is True

    def test_archive_is_host(self) -> None:
        assert _is_archive_today_snapshot_url(
            "https://archive.is/XyZ98"
        ) is True

    def test_submit_path_rejected(self) -> None:
        assert _is_archive_today_snapshot_url(
            "https://archive.today/submit/?url=https://example.com"
        ) is False

    def test_wip_path_rejected(self) -> None:
        assert _is_archive_today_snapshot_url(
            "https://archive.today/wip/abc"
        ) is False

    def test_homepage_rejected(self) -> None:
        assert _is_archive_today_snapshot_url(
            "https://archive.today/"
        ) is False

    def test_non_archive_host_rejected(self) -> None:
        assert _is_archive_today_snapshot_url(
            "https://example.com/foo"
        ) is False

    def test_garbage_rejected(self) -> None:
        assert _is_archive_today_snapshot_url("not a url") is False


class TestSaveToArchiveToday:
    async def test_success_returns_snapshot_url(self) -> None:
        page = _mock_page()
        page.url = "https://archive.today/abc12/https://example.com/"
        result = await save_to_archive_today("https://example.com/", page)
        assert result is not None
        assert "archive.today" in result

    async def test_goto_failure_returns_none(self) -> None:
        page = _mock_page()
        page.goto = AsyncMock(side_effect=RuntimeError("dns"))
        result = await save_to_archive_today("https://example.com/", page)
        assert result is None

    async def test_form_not_found_returns_none(self) -> None:
        """CF interstitial blocks form — locator wait_for times out."""
        page = _mock_page()
        page.locator.return_value.first.wait_for = AsyncMock(
            side_effect=TimeoutError("form never appeared")
        )
        result = await save_to_archive_today("https://example.com/", page)
        assert result is None

    async def test_form_failure_returns_none(self) -> None:
        page = _mock_page()
        page.fill = AsyncMock(side_effect=RuntimeError("no input"))
        result = await save_to_archive_today("https://example.com/", page)
        assert result is None

    async def test_wait_timeout_returns_none(self) -> None:
        page = _mock_page()
        page.wait_for_url = AsyncMock(
            side_effect=TimeoutError("captcha stuck")
        )
        result = await save_to_archive_today("https://example.com/", page)
        assert result is None


