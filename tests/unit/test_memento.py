# ABOUTME: Unit tests for the federated Memento tier (archiver.memento)
# ABOUTME: Mocks per-archive timemaps via respx; covers newest-wins and if_ fallback
"""Tests for archiver.memento."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from archiver.memento import (
    MEMENTO_ARCHIVES,
    MementoHit,
    _raw_replay_variants,
    fetch_memento_html,
    find_latest_memento,
)

_URL = "https://example.com/page"


@pytest.fixture(autouse=True)
def _no_guard_dns(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[misc]
    """SSRF guard must not do real DNS in unit tests."""
    monkeypatch.setattr(
        "archiver.http_client.check_url_safety_async",
        AsyncMock(return_value=None),
    )


def _timemap_body(memento_url: str, rfc822_dt: str) -> str:
    return (
        f'<{_URL}>; rel="original",\n'
        f'<{memento_url}>; rel="memento"; datetime="{rfc822_dt}",\n'
    )


def _mock_rosters(
    hits: dict[str, tuple[str, str]] | None = None,
    default_status: int = 404,
) -> None:
    """Mock every roster archive's timemap endpoint.

    `hits` maps archive id -> (memento_url, rfc822 datetime); all other
    archives respond with `default_status`.
    """
    hits = hits or {}
    for archive in MEMENTO_ARCHIVES:
        route = respx.get(archive.timemap_prefix + _URL)
        if archive.id in hits:
            memento_url, dt = hits[archive.id]
            route.mock(
                return_value=httpx.Response(
                    200, text=_timemap_body(memento_url, dt)
                )
            )
        else:
            route.mock(return_value=httpx.Response(default_status))


class TestRoster:
    def test_roster_is_nonempty_with_unique_ids(self) -> None:
        ids = [a.id for a in MEMENTO_ARCHIVES]
        assert len(ids) >= 5  # noqa: PLR2004
        assert len(ids) == len(set(ids))

    def test_roster_excludes_dedicated_tiers(self) -> None:
        """IA and archive.today have their own tiers — federating them
        here would double-query them on every memento-tier job."""
        for archive in MEMENTO_ARCHIVES:
            assert "web.archive.org/web" not in archive.timemap_prefix
            assert "archive.today" not in archive.timemap_prefix

    def test_prefixes_end_ready_for_url_append(self) -> None:
        for archive in MEMENTO_ARCHIVES:
            assert archive.timemap_prefix.endswith("/")


class TestFindLatestMemento:
    @respx.mock
    async def test_single_archive_hit(self) -> None:
        _mock_rosters({
            "arquivo.pt": (
                "https://arquivo.pt/wayback/20200101120000/" + _URL,
                "Wed, 01 Jan 2020 12:00:00 GMT",
            ),
        })
        hit = await find_latest_memento(_URL)
        assert hit is not None
        assert hit.archive_id == "arquivo.pt"
        assert hit.timestamp == datetime(2020, 1, 1, 12, 0, tzinfo=UTC)

    @respx.mock
    async def test_newest_across_archives_wins(self) -> None:
        _mock_rosters({
            "arquivo.pt": (
                "https://arquivo.pt/wayback/20150101000000/" + _URL,
                "Thu, 01 Jan 2015 00:00:00 GMT",
            ),
            "awa": (
                "https://web.archive.org.au/awa/20230601000000/" + _URL,
                "Thu, 01 Jun 2023 00:00:00 GMT",
            ),
        })
        hit = await find_latest_memento(_URL)
        assert hit is not None
        assert hit.archive_id == "awa"

    @respx.mock
    async def test_undated_memento_loses_to_dated(self) -> None:
        for archive in MEMENTO_ARCHIVES:
            route = respx.get(archive.timemap_prefix + _URL)
            if archive.id == "vefsafn":
                # Entry with an unparseable datetime attribute.
                route.mock(return_value=httpx.Response(
                    200,
                    text='<https://vefsafn.is/x>; rel="memento";'
                         ' datetime="not-a-date",\n',
                ))
            elif archive.id == "banq":
                route.mock(return_value=httpx.Response(
                    200,
                    text=_timemap_body(
                        "https://waext.banq.qc.ca/wayback/"
                        "20100101000000/" + _URL,
                        "Fri, 01 Jan 2010 00:00:00 GMT",
                    ),
                ))
            else:
                route.mock(return_value=httpx.Response(404))
        hit = await find_latest_memento(_URL)
        assert hit is not None
        assert hit.archive_id == "banq"

    @respx.mock
    async def test_all_miss_returns_none(self) -> None:
        _mock_rosters()
        assert await find_latest_memento(_URL) is None

    @respx.mock
    async def test_transport_errors_tolerated(self) -> None:
        """One archive down must not sink the whole federation query."""
        for archive in MEMENTO_ARCHIVES:
            route = respx.get(archive.timemap_prefix + _URL)
            if archive.id == "archive-it":
                route.mock(side_effect=httpx.ConnectError("down"))
            elif archive.id == "lac":
                route.mock(return_value=httpx.Response(
                    200,
                    text=_timemap_body(
                        "https://webarchiveweb.wayback.bac-lac.canada.ca"
                        "/web/20220101000000/" + _URL,
                        "Sat, 01 Jan 2022 00:00:00 GMT",
                    ),
                ))
            else:
                route.mock(return_value=httpx.Response(404))
        hit = await find_latest_memento(_URL)
        assert hit is not None
        assert hit.archive_id == "lac"

    @respx.mock
    async def test_timemap_without_mementos_is_miss(self) -> None:
        _mock_rosters()
        respx.get(MEMENTO_ARCHIVES[0].timemap_prefix + _URL).mock(
            return_value=httpx.Response(
                200, text=f'<{_URL}>; rel="original"\n'
            )
        )
        assert await find_latest_memento(_URL) is None


class TestRawReplayVariants:
    def test_inserts_if_flag_after_timestamp(self) -> None:
        variants = _raw_replay_variants(
            "https://arquivo.pt/wayback/20200101120000/https://x.example/"
        )
        assert variants == [
            "https://arquivo.pt/wayback/20200101120000if_/https://x.example/",
            "https://arquivo.pt/wayback/20200101120000/https://x.example/",
        ]

    def test_no_timestamp_yields_plain_only(self) -> None:
        assert _raw_replay_variants("https://perma.cc/AB12-CD34") == [
            "https://perma.cc/AB12-CD34"
        ]

    def test_only_first_timestamp_rewritten(self) -> None:
        variants = _raw_replay_variants(
            "https://a.example/20200101120000/b/20210101120000/c"
        )
        assert variants[0] == (
            "https://a.example/20200101120000if_/b/20210101120000/c"
        )


class TestFetchMementoHtml:
    @respx.mock
    async def test_prefers_raw_variant(self) -> None:
        raw = respx.get(
            "https://arquivo.pt/wayback/20200101120000if_/https://x.example/"
        ).mock(return_value=httpx.Response(200, text="<html>raw</html>"))
        plain = respx.get(
            "https://arquivo.pt/wayback/20200101120000/https://x.example/"
        ).mock(return_value=httpx.Response(200, text="<html>chrome</html>"))
        html = await fetch_memento_html(
            "https://arquivo.pt/wayback/20200101120000/https://x.example/"
        )
        assert html == "<html>raw</html>"
        assert raw.called
        assert not plain.called

    @respx.mock
    async def test_falls_back_when_raw_unsupported(self) -> None:
        respx.get(
            "https://a.example/20200101120000if_/https://x.example/"
        ).mock(return_value=httpx.Response(404))
        respx.get(
            "https://a.example/20200101120000/https://x.example/"
        ).mock(return_value=httpx.Response(200, text="<html>plain</html>"))
        html = await fetch_memento_html(
            "https://a.example/20200101120000/https://x.example/"
        )
        assert html == "<html>plain</html>"

    @respx.mock
    async def test_transport_error_falls_back(self) -> None:
        respx.get(
            "https://a.example/20200101120000if_/https://x.example/"
        ).mock(side_effect=httpx.ConnectError("nope"))
        respx.get(
            "https://a.example/20200101120000/https://x.example/"
        ).mock(return_value=httpx.Response(200, text="<html>ok</html>"))
        html = await fetch_memento_html(
            "https://a.example/20200101120000/https://x.example/"
        )
        assert html == "<html>ok</html>"

    @respx.mock
    async def test_all_variants_fail_returns_none(self) -> None:
        respx.get(
            "https://a.example/20200101120000if_/https://x.example/"
        ).mock(return_value=httpx.Response(403))
        respx.get(
            "https://a.example/20200101120000/https://x.example/"
        ).mock(return_value=httpx.Response(403))
        assert await fetch_memento_html(
            "https://a.example/20200101120000/https://x.example/"
        ) is None


class TestMementoHitModel:
    def test_hit_is_frozen(self) -> None:
        hit = MementoHit(
            archive_id="x", memento_url="https://y", timestamp=None
        )
        with pytest.raises(AttributeError):
            hit.archive_id = "z"  # type: ignore[misc]
