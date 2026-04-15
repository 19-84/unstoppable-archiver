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

DB_URL = os.environ.get(
    "ARCHIVER_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.pool.Pool]:
    p = await create_pool(DB_URL, min_size=2, max_size=5)
    await init_db(p)
    yield p
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM jobs")
        await conn.execute("DELETE FROM archives")
    await close_pool(p)


@pytest.fixture
async def client(
    pool: asyncpg.pool.Pool,
) -> AsyncIterator[AsyncClient]:
    settings = Settings(db_url=DB_URL, log_format="console")
    app = create_app(settings)
    app.state.pool = pool
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
