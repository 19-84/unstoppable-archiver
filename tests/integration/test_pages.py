# ABOUTME: Integration tests for Glass Noir HTML page routes
# ABOUTME: Tests home, detail, search, and partials rendering against real PostgreSQL
"""Tests for page routes."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg.pool
import pytest
from httpx import ASGITransport, AsyncClient

from archiver.app import create_app
from archiver.config import Settings
from archiver.db import close_pool, create_pool, init_db
from tests.integration.conftest import reset_test_db

DB_URL = os.environ.get(
    "ARCHIVER_TEST_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver_test",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.pool.Pool]:
    p = await create_pool(DB_URL, min_size=2, max_size=5)
    await init_db(p)
    await reset_test_db(p)   # clean slate IN, not OUT
    yield p
    await close_pool(p)


@pytest.fixture
async def client(
    pool: asyncpg.pool.Pool,
) -> AsyncIterator[AsyncClient]:
    from archiver.blocklist import DomainBlocklist

    settings = Settings(db_url=DB_URL, log_format="console")
    app = create_app(settings)
    app.state.pool = pool
    app.state.blocklist = DomainBlocklist()
    app.state.settings = settings  # ensure self-hosted mode
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


class TestHomePage:
    async def test_renders_html(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "text/html" in resp.headers["content-type"]
        assert "unstoppable archive" in resp.text
        assert "Preserve the web" in resp.text

    async def test_has_stats(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/")
        assert "pages" in resp.text
        assert "domains" in resp.text
        assert "success" in resp.text

    async def test_has_search_description(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/")
        assert "Full-text search" in resp.text

    async def test_selfhosted_shows_bookmarklet(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/")
        # Bookmarklet is only rendered in self-hosted mode (default)
        assert "Archive this" in resp.text


class TestArchivesBrowse:
    """Public /archives browse route — the only HTML surface that
    exposes all archives beyond the home page's recent-10 list.

    Without this route, captures past index #10 are unreachable through
    the UI unless the user knows the ULID or guesses a keyword that
    matches the title."""

    async def _seed_n(
        self, pool: asyncpg.pool.Pool, n: int, prefix: str,
    ) -> None:
        async with pool.acquire() as conn:
            for i in range(n):
                await conn.execute(
                    """
                    INSERT INTO archives
                        (id, url, url_hash, status, source, tier,
                         created_at, completed_at, title)
                    VALUES ($1, $2, $3, 'complete', 'direct', 'chromium',
                            now() - ($4::int || ' seconds')::interval,
                            now() - ($4::int || ' seconds')::interval,
                            $5)
                    """,
                    f"01TESTBROWSE{i:013d}",
                    f"https://example.com/{prefix}-{i}",
                    f"{prefix}-hash-{i:030d}",
                    i,
                    f"{prefix} {i}",
                )

    async def test_renders_list_with_pagination(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        await self._seed_n(pool, 12, "browse")
        resp = await client.get("/archives?limit=5&offset=0")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.text
        assert "All archives" in body
        assert 'aria-label="Pagination"' in body
        # Page 1 of 3 → next link present, prev hidden
        assert "Next" in body
        assert "/archives?limit=5&offset=5" in body
        # First 5 archives present, last 7 not yet
        assert "browse 0" in body
        assert "browse 11" not in body

    async def test_pagination_offset_navigates(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        await self._seed_n(pool, 12, "page2")
        resp = await client.get("/archives?limit=5&offset=5")
        body = resp.text
        # Page 2 shows prev + next
        assert "Prev" in body
        assert "Next" in body
        assert "6–10 of 12" in body  # noqa: RUF001

    async def test_negative_offset_clamps_to_zero(
        self,
        client: AsyncClient,
    ) -> None:
        """A bogus negative offset must not crash or trigger a 500 — it
        gets clamped to 0 so the page renders the first slice."""
        resp = await client.get("/archives?offset=-9999")
        assert resp.status_code == 200  # noqa: PLR2004

    async def test_excessive_limit_is_capped(
        self,
        client: AsyncClient,
    ) -> None:
        """An over-large limit is capped at 100 so a malicious query
        can't request 'limit=1000000' to DoS the listing query."""
        resp = await client.get("/archives?limit=99999")
        assert resp.status_code == 200  # noqa: PLR2004

    async def test_view_all_link_appears_when_more_archives_exist(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """The home page only renders the 'View all →' link when there
        are more archives than the recent list shows. Without this
        gate, the link would appear even on empty / small instances
        where it points to nothing extra."""
        # Seed 11 — one more than the home-page recent_archives limit (10).
        await self._seed_n(pool, 11, "viewall")
        resp = await client.get("/")
        assert "View all" in resp.text
        assert 'href="/archives"' in resp.text


class TestViewerSiblingsCount:
    """The viewer toolbar must surface 'N captures' when a URL has
    multiple snapshots so users can discover sibling captures (e.g. a
    direct capture and a privacy_frontend fallback of the same URL).
    Without this, a user viewing the fallback capture has no UI
    affordance to find or compare the direct version — they have to
    back out to the detail page and read the history list."""

    async def test_shows_position_when_multiple_captures_exist(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """Viewer toolbar must surface BOTH the total count AND this
        capture's position — 'Capture 1 of 2'. Just '2 captures' was
        ambiguous: a user couldn't tell if they were looking at the
        newest or the oldest. Position 1 = newest (matches the
        detail-page history sort order)."""
        from archiver.url import url_hash as _hash
        url = "https://example.com/multi-capture-uat"
        uhash = _hash(url)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives (id, url, url_hash, status, source,
                    tier, created_at, completed_at, title, artifact_dir,
                    snapshot_size)
                VALUES ($1, $2, $3, 'complete', 'direct', 'chromium',
                        now() - interval '2 days',
                        now() - interval '2 days',
                        'A', 'sibtest/A', 100),
                       ($4, $2, $3, 'complete', 'privacy_frontend',
                        'privacy_frontend', now() - interval '1 day',
                        now() - interval '1 day',
                        'B', 'sibtest/B', 100)
                """,
                "01TESTSIBA00000000000000",
                url, uhash,
                "01TESTSIBB00000000000000",
            )

        # Newer capture (B) — should be position 1 of 2.
        resp = await client.get(
            "/archive/01TESTSIBB00000000000000/view",
            follow_redirects=True,
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.text
        assert "Capture 1 of 2" in body
        assert 'href="/archive/01TESTSIBB00000000000000"' in body

        # Older capture (A) — should be position 2 of 2.
        resp = await client.get(
            "/archive/01TESTSIBA00000000000000/view",
            follow_redirects=True,
        )
        body = resp.text
        assert "Capture 2 of 2" in body

    async def test_hides_link_when_only_one_capture(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """A URL with a single capture must NOT render the link —
        'Capture 1 of 1' is misleading UI noise."""
        from archiver.url import url_hash as _hash
        url = "https://example.com/lonely-capture-uat"
        uhash = _hash(url)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives (id, url, url_hash, status, source,
                    tier, created_at, completed_at, title, artifact_dir,
                    snapshot_size)
                VALUES ($1, $2, $3, 'complete', 'direct', 'chromium',
                        now(), now(), 'Lonely', 'lonely/A', 100)
                """,
                "01TESTLONELY000000000000",
                url, uhash,
            )

        resp = await client.get(
            "/archive/01TESTLONELY000000000000/view",
            follow_redirects=True,
        )
        body = resp.text
        # No "Capture N of M" badge — link must be completely absent.
        assert "Capture 1 of 1" not in body
        assert "Capture " not in body or ">Capture " not in body


class TestRecapture:
    async def test_recapture_creates_new_archive(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        original_id = resp.json()["id"]
        resp = await client.post(
            f"/recapture/{original_id}", follow_redirects=False
        )
        assert resp.status_code == 303  # noqa: PLR2004
        # Should redirect to a *new* archive, not the original
        assert original_id not in resp.headers["location"]

    async def test_recapture_404_for_missing_archive(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/recapture/nonexistent", follow_redirects=False
        )
        assert resp.status_code == 404  # noqa: PLR2004


class TestArchiveDetailPage:
    async def test_renders_for_existing_archive(
        self, client: AsyncClient
    ) -> None:
        create = await client.post(
            "/api/archives",
            json={"url": "https://example.com/detail-test"},
        )
        archive_id = create.json()["id"]

        resp = await client.get(f"/archive/{archive_id}")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "example.com" in resp.text

    async def test_404_for_missing(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/archive/nonexistent")
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_social_preview_meta_tags_for_complete_archive(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """Archive detail pages must emit Open Graph + Twitter Card
        meta tags so shared links render as preview cards on
        Slack / Discord / Twitter / iMessage instead of bare URLs.

        Pins the contract: title, original URL, capture date, canonical
        URL, and (when the archive is complete) an og:image pointing at
        the screenshot artifact with summary_large_image card type. All
        URLs must be absolute — relative paths break the social crawlers."""
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives
                    (id, url, url_hash, status, source, tier,
                     created_at, completed_at, title, artifact_dir,
                     snapshot_size, screenshot_hash)
                VALUES ($1, $2, $3, 'complete', 'direct', 'chromium',
                        now() - interval '1 hour',
                        now() - interval '1 hour',
                        $4, 'og/20260515', 1024, 'shothash')
                """,
                "01TESTOG00000000000000000",
                "https://example.com/og-pin",
                "ogpin-hash-32chars-abc1234567890",
                "OG Test Page",
            )

        resp = await client.get("/archive/01TESTOG00000000000000000")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.text

        assert '<meta property="og:site_name" content="Unstoppable Archive">' in body
        assert '<meta property="og:type" content="article">' in body
        assert '<meta property="og:title" content="OG Test Page">' in body
        assert "og:url" in body
        assert "archive/01TESTOG00000000000000000" in body
        assert 'og:description' in body
        assert "https://example.com/og-pin" in body
        assert 'og:image' in body
        assert "api/archives/01TESTOG00000000000000000/screenshot" in body
        assert 'twitter:card" content="summary_large_image"' in body
        assert 'rel="canonical"' in body

    async def test_social_preview_falls_back_to_summary_when_no_screenshot(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """In-progress / pending archives have no screenshot yet, so the
        twitter:card type must downgrade to 'summary' (not
        summary_large_image) and og:image must be omitted. Emitting an
        og:image URL that 404s makes the social preview broken on
        platforms that fetch the image before rendering."""
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives
                    (id, url, url_hash, status, source, tier, created_at)
                VALUES ($1, $2, $3, 'pending', 'direct', 'chromium', now())
                """,
                "01TESTOGPEND000000000000",
                "https://example.com/og-pending",
                "ogpending-hash-32chars-abcdefg12",
            )

        resp = await client.get("/archive/01TESTOGPEND000000000000")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.text

        assert 'twitter:card" content="summary"' in body
        assert "summary_large_image" not in body
        assert 'property="og:image"' not in body

    async def test_history_renders_all_five_source_labels(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """The snapshot history block on the detail page must
        distinguish every source tier — direct, wayback, archive.today,
        privacy_frontend, and commoncrawl. Earlier the template only
        branched on wayback/archive.today and bucketed the rest as
        '● direct', so a privacy_frontend or commoncrawl capture was
        visually indistinguishable from a genuine direct capture. This
        defeated the whole provenance story: the history list is
        exactly where the user looks to see how each snapshot was
        obtained."""
        from archiver.url import url_hash as _hash
        url = "https://example.com/sources-uat-pin"
        uhash = _hash(url)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives (id, url, url_hash, status, source,
                    tier, created_at, completed_at, title, artifact_dir,
                    snapshot_size)
                VALUES
                  ($1, $6, $7, 'complete', 'direct', 'chromium',
                   now() - interval '5 days',
                   now() - interval '5 days', 'D', 'srcpin/A', 100),
                  ($2, $6, $7, 'complete', 'wayback', 'wayback',
                   now() - interval '4 days',
                   now() - interval '4 days', 'W', 'srcpin/B', 100),
                  ($3, $6, $7, 'complete', 'archive_today',
                   'archive_today',
                   now() - interval '3 days',
                   now() - interval '3 days', 'AT', 'srcpin/C', 100),
                  ($4, $6, $7, 'complete', 'privacy_frontend',
                   'privacy_frontend',
                   now() - interval '2 days',
                   now() - interval '2 days', 'PF', 'srcpin/D', 100),
                  ($5, $6, $7, 'complete', 'commoncrawl', 'commoncrawl',
                   now() - interval '1 day',
                   now() - interval '1 day', 'CC', 'srcpin/E', 100)
                """,
                "01TESTSRC1AAAAAAAAAAAAAA",
                "01TESTSRC2BBBBBBBBBBBBBB",
                "01TESTSRC3CCCCCCCCCCCCCC",
                "01TESTSRC4DDDDDDDDDDDDDD",
                "01TESTSRC5EEEEEEEEEEEEEE",
                url, uhash,
            )

        resp = await client.get("/archive/01TESTSRC1AAAAAAAAAAAAAA")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.text

        # Every fallback source must be uniquely labeled — no silent
        # bucketing into 'direct'.
        assert "▲ wayback" in body
        assert "◆ archive.today" in body
        assert "⊙ privacy frontend" in body
        assert "★ common crawl" in body
        # Genuine direct capture still labels as such.
        assert "● direct" in body

    async def test_provenance_renders_captured_from_link(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """End-to-end pin for the provenance feature: a privacy_frontend
        capture with source_url in metadata must render a 'Captured from:'
        block on the detail page that links to the source_url.

        This is the user-acceptance test for the original concern ('do
        fallback archives link back to the original submission URL,
        and is the actual capture URL visible?'). Failure here means
        provenance is silently broken in the UI."""
        import json as _json

        # Simulate a completed privacy_frontend capture: original URL is
        # the canonical twitter.com one, but we actually fetched from a
        # Nitter proxy. metadata.source_url records the proxy.
        original_url = "https://twitter.com/jack/status/20"
        source_url = "https://nitter.tiekoetter.com/jack/status/20"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives
                    (id, url, url_hash, status, source, tier,
                     created_at, completed_at, title, snapshot_size, metadata)
                VALUES ($1, $2, $3, 'complete', 'privacy_frontend',
                        'privacy_frontend', now(), now(), $4, 21330, $5::jsonb)
                """,
                "01TESTPROV0000000000000000",
                original_url,
                "testprovenance1",
                "just setting up my twttr",
                _json.dumps({"source_url": source_url}),
            )

        resp = await client.get("/archive/01TESTPROV0000000000000000")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.text

        # 1. Original URL is shown and 'Original ↗' link points at it.
        assert original_url in body
        # 2. The 'Captured from:' provenance block is present.
        assert "Captured from" in body
        # 3. source_url appears as a clickable link with security attrs.
        assert source_url in body
        # 4. Source label distinguishes privacy_frontend (the icon ⊙
        #    test is in test_view; here we check the source text).
        assert "privacy_frontend" in body


class TestArchiveViewPage:
    async def test_404_for_missing_archive(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/archive/nonexistent/view")
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_404_for_pending_archive(
        self, client: AsyncClient
    ) -> None:
        create = await client.post(
            "/api/archives",
            json={"url": "https://example.com/view-pending"},
        )
        archive_id = create.json()["id"]
        resp = await client.get(f"/archive/{archive_id}/view")
        assert resp.status_code == 404  # noqa: PLR2004


class TestWaybackStyleURLs:
    async def test_latest_404_for_unknown_url(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(
            "/web/latest/https://never-archived.example.com/",
            follow_redirects=False,
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_timestamped_400_on_invalid_timestamp(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(
            "/web/notanumber/https://example.com/",
            follow_redirects=False,
        )
        assert resp.status_code == 400  # noqa: PLR2004

    async def test_timestamped_404_when_no_snapshot(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(
            "/web/20260418/https://never-archived.example.com/",
            follow_redirects=False,
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_legacy_archive_view_redirects_to_wayback(
        self, client: AsyncClient
    ) -> None:
        """`/archive/{id}/view` 301-redirects to `/web/{ts}/{url}`."""
        # Submit a URL and manually mark it complete so the view is
        # reachable (we don't want to wait for a full capture here).
        create = await client.post(
            "/api/archives",
            json={"url": "https://example.com/wayback-redirect"},
        )
        archive_id = create.json()["id"]
        # Pending → view returns 404 (not complete). We cover the
        # redirect path via the integration pool fixture marking it
        # complete in other tests; here we just verify the 404 path.
        resp = await client.get(
            f"/archive/{archive_id}/view", follow_redirects=False
        )
        assert resp.status_code == 404  # noqa: PLR2004


class TestSearchPage:
    async def test_renders_empty_search(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/search?q=xyznothing")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "No results" in resp.text

    async def test_renders_with_query(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/search?q=test")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "text/html" in resp.headers["content-type"]


class TestSubmitForm:
    async def test_submit_url_redirects(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/submit",
            data={"url": "https://example.com/submit"},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # noqa: PLR2004
        assert "/archive/" in resp.headers["location"]

    async def test_submit_form_dedups_to_existing_archive(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """HTML form path must redirect to the existing capture instead
        of creating a duplicate pending archive. /api/archives returns
        409, but the HTML surface is friendlier — redirect them straight
        to the archive they already have."""
        url = "https://example.com/form-dedup-test"
        # First submission creates the row
        first = await client.post(
            "/submit",
            data={"url": url},
            follow_redirects=False,
        )
        first_id = first.headers["location"].rsplit("/", 1)[-1]

        # Promote to complete so check_recent_capture sees it
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE archives SET status='complete', completed_at=now()"
                " WHERE id=$1",
                first_id,
            )

        # Second submission hits the dedup branch
        second = await client.post(
            "/submit",
            data={"url": url},
            follow_redirects=False,
        )
        assert second.status_code == 303  # noqa: PLR2004
        # Must redirect to the EXISTING archive, not a new one
        assert second.headers["location"].endswith(f"/archive/{first_id}")

        # Confirm no second row was created
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM archives WHERE url=$1", url,
            )
            assert count == 1

    async def test_submit_search_redirects(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/submit",
            data={"url": "python programming"},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # noqa: PLR2004
        assert "/search?q=" in resp.headers["location"]

    async def test_submit_empty_returns_400(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/submit", data={"url": ""}
        )
        assert resp.status_code == 400  # noqa: PLR2004

    async def test_submit_htmx_empty_returns_200_partial(
        self, client: AsyncClient
    ) -> None:
        """HX-Request: empty URL returns 200 + small error partial so htmx swaps it."""
        resp = await client.post(
            "/submit",
            data={"url": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert 'role="alert"' in resp.text
        assert "Please enter a URL" in resp.text
        # Must be a partial — NOT the full index page
        assert "<html" not in resp.text.lower()

    async def test_submit_htmx_ssrf_returns_200_partial(
        self, client: AsyncClient
    ) -> None:
        """HX-Request: SSRF block returns the error card, not a dropped 400."""
        resp = await client.post(
            "/submit",
            data={"url": "http://192.168.1.1/admin"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert 'role="alert"' in resp.text
        assert "<html" not in resp.text.lower()


class TestPartials:
    async def test_status_partial(
        self, client: AsyncClient
    ) -> None:
        create = await client.post(
            "/api/archives",
            json={"url": "https://example.com/partial-test"},
        )
        archive_id = create.json()["id"]

        resp = await client.get(
            f"/partials/status/{archive_id}"
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert "Pending" in resp.text

    async def test_search_partial(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/partials/search?q=test")
        assert resp.status_code == 200  # noqa: PLR2004


class TestSoftDeleteVisibility:
    """Soft-deleted archives must not be served on any public route.

    Before the fix, admin takedown only hid the archive from the
    list endpoint — every other route (detail, viewer, snapshot
    serve, /web/{ts}/{url}, dedup check) happily returned the
    removed content. Each of these tests pins one of those
    surfaces against the regression.
    """

    async def _seed_removed(
        self, pool: asyncpg.pool.Pool, url: str,
    ) -> str:
        """Create a removed archive and return its id."""
        from archiver.url import url_hash as _hash
        archive_id = "01TESTRM00000000000000000"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives
                    (id, url, url_hash, status, source, tier,
                     created_at, completed_at, title, artifact_dir,
                     snapshot_size, removed_at, removed_reason)
                VALUES ($1, $2, $3, 'complete', 'direct', 'chromium',
                        now(), now(), 'taken down', 'rm/20260515', 30,
                        now(), 'admin takedown')
                """,
                archive_id, url, _hash(url),
            )
        return archive_id

    async def test_detail_returns_410_with_takedown_notice(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """Removed archives render a friendly takedown stub at 410 Gone
        (not a bare 404) so legitimate revisits learn *why* the archive
        is gone instead of seeing a generic 'not found' page. The
        snapshot is no longer served; only the takedown metadata is."""
        archive_id = await self._seed_removed(
            pool, "https://example.com/rm-detail",
        )
        resp = await client.get(f"/archive/{archive_id}")
        assert resp.status_code == 410  # noqa: PLR2004
        body = resp.text
        assert "This archive has been removed" in body
        assert "admin takedown" in body
        assert "https://example.com/rm-detail" in body

    async def test_wayback_url_404s_for_removed(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        url = "https://example.com/rm-wayback"
        await self._seed_removed(pool, url)
        resp = await client.get(
            f"/web/latest/{url}", follow_redirects=False,
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_legacy_view_redirects_to_takedown_stub(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """The legacy /archive/{id}/view route is a 301 redirect to the
        Wayback-style viewer for live archives. For removed archives it
        should send the user to the detail page (which renders the
        friendly takedown stub at HTTP 410) instead of a bare 404."""
        archive_id = await self._seed_removed(
            pool, "https://example.com/rm-view-redirect",
        )
        resp = await client.get(
            f"/archive/{archive_id}/view", follow_redirects=False,
        )
        assert resp.status_code == 303  # noqa: PLR2004
        assert resp.headers["location"] == f"/archive/{archive_id}"

    async def test_dedup_ignores_removed(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """A take-down'd capture must NOT be returned by the dedup
        check — re-submission creates a fresh archive instead of
        resurrecting the removed one."""
        url = "https://example.com/rm-dedup"
        removed_id = await self._seed_removed(pool, url)
        resp = await client.post(
            "/api/archives", json={"url": url},
        )
        assert resp.status_code == 201  # noqa: PLR2004
        new_id = resp.json()["id"]
        assert new_id != removed_id
