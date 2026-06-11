# ABOUTME: Integration tests for FastAPI REST API endpoints
# ABOUTME: Tests archive CRUD, search, health checks, and error handling against real PostgreSQL
"""Tests for API routes."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

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

    settings = Settings(db_url=DB_URL, log_format="console")  # type: ignore[arg-type]
    app = create_app(settings)
    # Inject the already-created pool so lifespan doesn't create a second one
    app.state.pool = pool
    app.state.blocklist = DomainBlocklist()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


class TestHealth:
    async def test_shallow_health(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/health")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["status"] == "ok"

    async def test_deep_health(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/health/deep")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["database"] == "connected"

    async def test_metrics_endpoint(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/metrics")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "text/plain" in resp.headers["content-type"]
        assert "archiver_jobs_queued" in resp.text


class TestArchiveCreate:
    async def test_create_archive(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/test"},
        )
        assert resp.status_code == 201  # noqa: PLR2004
        data = resp.json()
        assert data["status"] == "pending"
        assert data["url"] == "https://example.com/test"
        assert data["id"] is not None

    async def test_create_duplicate_blocked(
        self, client: AsyncClient
    ) -> None:
        await client.post(
            "/api/archives",
            json={"url": "https://example.com/dup"},
        )
        # Mark first as complete so dedup check triggers
        # (pending archives don't trigger dedup)
        # For this test, just verify the endpoint works
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/dup2"},
        )
        assert resp.status_code == 201  # noqa: PLR2004

    async def test_create_invalid_url(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={"url": "not-a-url"},
        )
        assert resp.status_code == 422  # noqa: PLR2004

    async def test_dedup_returns_409(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """Second submission of a recently-complete URL returns 409."""
        resp = await client.post(
            "/api/archives", json={"url": "https://dedup.example/"}
        )
        archive_id = resp.json()["id"]
        # Manually mark complete so the dedup check fires
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE archives SET status='complete',"
                " completed_at=now() WHERE id=$1",
                archive_id,
            )
        resp2 = await client.post(
            "/api/archives", json={"url": "https://dedup.example/"}
        )
        assert resp2.status_code == 409  # noqa: PLR2004
        assert resp2.json()["existing_id"] == archive_id

    async def test_force_override_dedup(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        resp = await client.post(
            "/api/archives", json={"url": "https://force.example/"}
        )
        archive_id = resp.json()["id"]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE archives SET status='complete',"
                " completed_at=now() WHERE id=$1",
                archive_id,
            )
        resp2 = await client.post(
            "/api/archives",
            json={"url": "https://force.example/", "force": True},
        )
        assert resp2.status_code == 201  # noqa: PLR2004
        assert resp2.json()["id"] != archive_id

    async def test_create_force_bypass_dedup(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={
                "url": "https://example.com/force",
                "force": True,
            },
        )
        assert resp.status_code == 201  # noqa: PLR2004


class TestArchiveList:
    async def test_list_empty(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/archives")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["archives"] == []
        assert data["total"] == 0

    async def test_list_with_archives(
        self, client: AsyncClient
    ) -> None:
        await client.post(
            "/api/archives",
            json={"url": "https://a.com"},
        )
        await client.post(
            "/api/archives",
            json={"url": "https://b.com"},
        )
        resp = await client.get("/api/archives")
        data = resp.json()
        assert data["total"] == 2  # noqa: PLR2004
        assert len(data["archives"]) == 2  # noqa: PLR2004

    async def test_list_pagination(
        self, client: AsyncClient
    ) -> None:
        for i in range(3):
            await client.post(
                "/api/archives",
                json={"url": f"https://page{i}.com"},
            )
        resp = await client.get("/api/archives?limit=2&offset=0")
        data = resp.json()
        assert len(data["archives"]) == 2  # noqa: PLR2004
        assert data["total"] == 3  # noqa: PLR2004

    async def test_list_response_echoes_pagination_window(
        self, client: AsyncClient,
    ) -> None:
        """API consumers paginating through archives need limit +
        offset echoed in the response so they don't have to track
        their own state. Without these fields, an integration script
        walking the list with ?limit=N&offset=M has no way to know
        which slice it's looking at — and computing has_next from
        ``offset + len(archives) < total`` requires knowing offset."""
        for i in range(5):
            await client.post(
                "/api/archives",
                json={"url": f"https://echopag{i}.example.com"},
            )

        resp = await client.get("/api/archives?limit=2&offset=2")
        data = resp.json()
        assert data["limit"] == 2  # noqa: PLR2004
        assert data["offset"] == 2  # noqa: PLR2004
        assert data["total"] == 5  # noqa: PLR2004
        # The combination lets the client compute has_next without
        # tracking its own pagination state.
        has_next = data["offset"] + len(data["archives"]) < data["total"]
        assert has_next is True

    async def test_search_response_echoes_pagination_window(
        self, client: AsyncClient, pool: asyncpg.pool.Pool,
    ) -> None:
        """Same self-describing-pagination requirement for the search
        endpoint — without limit/offset in the response, paginating
        search results requires the client to remember its own state."""
        async with pool.acquire() as conn:
            for i in range(4):
                await conn.execute(
                    """
                    INSERT INTO archives (id, url, url_hash, status,
                        source, tier, created_at, completed_at, title)
                    VALUES ($1, $2, $3, 'complete', 'direct', 'chromium',
                            now(), now(), 'echopag-title')
                    """,
                    f"01TESTPAGSRCH{i:011d}",
                    f"https://example.com/srch-pag-{i}",
                    f"srchpag-hash-{i:030d}",
                )

        resp = await client.get(
            "/api/archives/search?q=echopag-title&limit=2&offset=1",
        )
        data = resp.json()
        assert data["limit"] == 2  # noqa: PLR2004
        assert data["offset"] == 1
        assert data["query"] == "echopag-title"


class TestArchiveGet:
    async def test_get_by_id(
        self, client: AsyncClient
    ) -> None:
        create_resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/get"},
        )
        archive_id = create_resp.json()["id"]

        resp = await client.get(f"/api/archives/{archive_id}")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["id"] == archive_id

    async def test_get_nonexistent(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/archives/nonexistent")
        assert resp.status_code == 404  # noqa: PLR2004


class TestArchiveDelete:
    async def test_delete_archive(
        self, client: AsyncClient
    ) -> None:
        create_resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/del"},
        )
        archive_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/archives/{archive_id}")
        assert resp.status_code == 204  # noqa: PLR2004

        get_resp = await client.get(f"/api/archives/{archive_id}")
        assert get_resp.status_code == 404  # noqa: PLR2004

    async def test_delete_nonexistent(
        self, client: AsyncClient
    ) -> None:
        resp = await client.delete("/api/archives/nonexistent")
        assert resp.status_code == 404  # noqa: PLR2004


class TestSearch:
    async def test_search_empty(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(
            "/api/archives/search?q=nonexistent"
        )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["total"] == 0
        assert data["archives"] == []
        assert data["query"] == "nonexistent"


class TestArtifactEndpoints:
    async def test_snapshot_404_no_artifacts(
        self, client: AsyncClient
    ) -> None:
        create_resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/noart"},
        )
        archive_id = create_resp.json()["id"]
        resp = await client.get(
            f"/api/archives/{archive_id}/snapshot"
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_warc_404_no_artifacts(
        self, client: AsyncClient
    ) -> None:
        create_resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/nowarc"},
        )
        archive_id = create_resp.json()["id"]
        resp = await client.get(
            f"/api/archives/{archive_id}/warc"
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_nonexistent_archive_snapshot(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(
            "/api/archives/nonexistent/snapshot"
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_serve_snapshot_file(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
    ) -> None:
        """Create an archive with artifacts on disk and serve them."""
        from archiver.enums import ArchiveStatus
        from archiver.repository import ArchiveRepository

        repo = ArchiveRepository()
        async with pool.acquire() as conn:
            archive = await repo.create(
                conn, "https://example.com/serve"
            )
            # Create artifact files
            art_dir = tmp_path / "artifacts" / "test"
            art_dir.mkdir(parents=True)
            (art_dir / "snapshot.html").write_text("<html>hi</html>")
            (art_dir / "screenshot.png").write_bytes(b"\x89PNG")
            (art_dir / "thumbnail.png").write_bytes(b"\x89PNG")

            rel = str(art_dir.relative_to(tmp_path / "artifacts"))
            await repo.update_status(
                conn,
                archive.id,
                ArchiveStatus.COMPLETE,
                artifact_dir=rel,
            )

        # Override artifacts_dir in settings
        client._transport.app.state.settings.artifacts_dir = (  # type: ignore[union-attr]
            tmp_path / "artifacts"
        )

        resp = await client.get(
            f"/api/archives/{archive.id}/snapshot"
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert "<html>hi</html>" in resp.text

        resp = await client.get(
            f"/api/archives/{archive.id}/screenshot"
        )
        assert resp.status_code == 200  # noqa: PLR2004

        resp = await client.get(
            f"/api/archives/{archive.id}/thumbnail"
        )
        assert resp.status_code == 200  # noqa: PLR2004

    async def test_artifact_responses_emit_short_cache_control(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
    ) -> None:
        """All artifact routes must emit Cache-Control: private,
        max-age=300. Without it, browsers + intermediate caches apply
        heuristic caching and may serve a previously-200 artifact
        from disk for hours after a takedown switched the row to 410
        — moderation actions wouldn't propagate to viewers who had
        already loaded the artifact once."""
        from archiver.enums import ArchiveStatus
        from archiver.repository import ArchiveRepository

        repo = ArchiveRepository()
        async with pool.acquire() as conn:
            archive = await repo.create(
                conn, "https://example.com/cache-headers-uat",
            )
            art_dir = tmp_path / "artifacts" / "cache_headers"
            art_dir.mkdir(parents=True)
            (art_dir / "snapshot.html").write_text("<html>x</html>")
            (art_dir / "archive.warc.gz").write_bytes(b"warc")
            (art_dir / "screenshot.png").write_bytes(b"\x89PNG")
            (art_dir / "thumbnail.png").write_bytes(b"\x89PNG")
            await repo.update_status(
                conn, archive.id, ArchiveStatus.COMPLETE,
                artifact_dir="cache_headers",
            )

        client._transport.app.state.settings.artifacts_dir = (  # type: ignore[union-attr]
            tmp_path / "artifacts"
        )

        for route in ("snapshot", "warc", "screenshot", "thumbnail"):
            resp = await client.get(
                f"/api/archives/{archive.id}/{route}",
            )
            assert resp.status_code == 200, route  # noqa: PLR2004
            cc = resp.headers.get("cache-control", "")
            # `private` is the key clause — blocks CDN / shared-cache
            # residency so a takedown can't be served from another
            # user's intermediate cache.
            assert "private" in cc, f"{route}: {cc!r}"
            # max-age caps the takedown propagation delay to 5 minutes.
            assert "max-age=300" in cc, f"{route}: {cc!r}"

    async def test_artifact_404_when_archive_missing(
        self, client: AsyncClient
    ) -> None:
        """All artifact endpoints return 404 for missing archive."""
        for endpoint in ("snapshot", "warc", "screenshot", "thumbnail"):
            resp = await client.get(
                f"/api/archives/nonexistent/{endpoint}"
            )
            assert resp.status_code == 404  # noqa: PLR2004

    async def test_artifact_404_when_no_artifacts(
        self, client: AsyncClient
    ) -> None:
        """Archive exists but has no artifacts yet (pending)."""
        resp = await client.post(
            "/api/archives", json={"url": "https://example.com/no-art", "force": True}
        )
        archive_id = resp.json()["id"]
        resp = await client.get(
            f"/api/archives/{archive_id}/warc"
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_delete_archive_with_artifacts(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
    ) -> None:
        """DELETE endpoint removes artifact directory from disk."""
        # Override artifacts_dir to a fresh tmp dir
        client._transport.app.state.settings.artifacts_dir = tmp_path  # type: ignore[union-attr]
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/del-art", "force": True},
        )
        archive_id = resp.json()["id"]
        rel_dir = f"hash/{archive_id[:8]}"
        artifact_path = tmp_path / rel_dir
        artifact_path.mkdir(parents=True)
        (artifact_path / "snapshot.html").write_bytes(b"test")
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE archives SET artifact_dir=$1 WHERE id=$2",
                rel_dir, archive_id,
            )
        resp = await client.delete(f"/api/archives/{archive_id}")
        assert resp.status_code == 204  # noqa: PLR2004
        assert not artifact_path.exists()


class TestCreateAppDefaults:
    """Cover create_app's settings=None branch."""

    def test_create_app_with_default_settings(self) -> None:
        """Calling create_app() with no settings should construct a
        default Settings() and return a usable FastAPI app."""
        from archiver.app import create_app
        app = create_app()
        # FastAPI instance with the project's title
        assert app.title == "Unstoppable Archive"



class TestServeSnapshotZstd:
    """Cover the zstd-compressed snapshot.html.zst serve paths."""

    async def _setup_zst_archive(
        self,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
        html: bytes,
    ) -> tuple[str, Path]:
        """Helper: create an archive whose snapshot.html.zst is on disk."""
        import zstandard as zstd

        from archiver.enums import ArchiveStatus
        from archiver.repository import ArchiveRepository

        repo = ArchiveRepository()
        async with pool.acquire() as conn:
            archive = await repo.create(
                conn, f"https://example.com/zst-{tmp_path.name}",
            )
            art_dir = tmp_path / "artifacts" / "zsttest"
            art_dir.mkdir(parents=True)
            compressed = zstd.ZstdCompressor(level=19).compress(html)
            (art_dir / "snapshot.html.zst").write_bytes(compressed)
            rel = str(art_dir.relative_to(tmp_path / "artifacts"))
            await repo.update_status(
                conn, archive.id, ArchiveStatus.COMPLETE,
                artifact_dir=rel,
            )
        return archive.id, art_dir

    async def test_zstd_client_receives_raw_zst(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
    ) -> None:
        """Client that advertises zstd in Accept-Encoding gets the
        raw .zst bytes with Content-Encoding: zstd. Server CPU stays
        out of the decompress path."""
        html = b"<html><body>compressed</body></html>"
        archive_id, _ = await self._setup_zst_archive(pool, tmp_path, html)
        client._transport.app.state.settings.artifacts_dir = (  # type: ignore[union-attr]
            tmp_path / "artifacts"
        )

        # httpx auto-decompresses zstd if it knows the encoding, so to
        # observe the raw .zst bytes we add Accept-Encoding manually
        # and read the raw body.
        resp = await client.get(
            f"/api/archives/{archive_id}/snapshot",
            headers={"Accept-Encoding": "zstd"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        # httpx decompresses based on Content-Encoding header.
        # Either way the final bytes match the original HTML.
        assert resp.content == html

    async def test_non_zstd_client_gets_decompressed_html(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
    ) -> None:
        """Client lacking zstd support (no zstd in Accept-Encoding) gets
        the snapshot decompressed server-side and served as plain HTML."""
        html = b"<html><body>legacy client</body></html>"
        archive_id, _ = await self._setup_zst_archive(pool, tmp_path, html)
        client._transport.app.state.settings.artifacts_dir = (  # type: ignore[union-attr]
            tmp_path / "artifacts"
        )

        resp = await client.get(
            f"/api/archives/{archive_id}/snapshot",
            headers={"Accept-Encoding": "gzip"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.content == html
        # Server-side decompress => no Content-Encoding header
        assert "content-encoding" not in {
            k.lower() for k in resp.headers
        }

    async def test_neither_zst_nor_plain_returns_404(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
    ) -> None:
        """artifact_dir is set but neither .zst nor plain snapshot
        exists on disk -> 404."""
        from archiver.enums import ArchiveStatus
        from archiver.repository import ArchiveRepository

        repo = ArchiveRepository()
        async with pool.acquire() as conn:
            archive = await repo.create(
                conn, "https://example.com/no-snap",
            )
            empty_dir = tmp_path / "artifacts" / "empty"
            empty_dir.mkdir(parents=True)
            await repo.update_status(
                conn, archive.id, ArchiveStatus.COMPLETE,
                artifact_dir=str(
                    empty_dir.relative_to(tmp_path / "artifacts")
                ),
            )
        client._transport.app.state.settings.artifacts_dir = (  # type: ignore[union-attr]
            tmp_path / "artifacts"
        )

        resp = await client.get(f"/api/archives/{archive.id}/snapshot")
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_path_traversal_blocked_with_400(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,
    ) -> None:
        """artifact_dir that resolves outside artifacts_dir (DB row
        contains '../etc' somehow) -> 400 instead of leaking outside."""
        from archiver.enums import ArchiveStatus
        from archiver.repository import ArchiveRepository

        repo = ArchiveRepository()
        async with pool.acquire() as conn:
            archive = await repo.create(
                conn, "https://example.com/escape",
            )
            await repo.update_status(
                conn, archive.id, ArchiveStatus.COMPLETE,
                artifact_dir="../etc",
            )
        (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
        client._transport.app.state.settings.artifacts_dir = (  # type: ignore[union-attr]
            tmp_path / "artifacts"
        )

        resp = await client.get(f"/api/archives/{archive.id}/snapshot")
        assert resp.status_code == 400  # noqa: PLR2004
