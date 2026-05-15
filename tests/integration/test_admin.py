# ABOUTME: Integration tests for admin auth, moderation, and report workflow
# ABOUTME: Tests login, takedown, audit log, blocklist reload against real PostgreSQL
"""Integration tests for admin routes + reports."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg.pool
import pytest
from httpx import ASGITransport, AsyncClient

from archiver.app import create_app
from archiver.auth import hash_password
from archiver.config import Settings
from archiver.db import close_pool, create_pool, init_db
from tests.integration.conftest import reset_test_db

DB_URL = os.environ.get(
    "ARCHIVER_TEST_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver_test",
)

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "test-admin-password"  # noqa: S105


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

    settings = Settings(
        db_url=DB_URL,
        log_format="console",
        admin_password_hash=hash_password(ADMIN_PASSWORD),  # type: ignore[arg-type]
        session_secret="test-session-secret-min-32-bytes-xxxxxxx",  # noqa: S106  # type: ignore[arg-type]
    )
    app = create_app(settings)
    app.state.pool = pool
    app.state.blocklist = DomainBlocklist()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
async def logged_in_client(client: AsyncClient) -> AsyncClient:
    """Return a client that has authenticated as admin."""
    resp = await client.post(
        "/admin/login",
        data={"password": ADMIN_PASSWORD, "next": "/admin/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303  # noqa: PLR2004
    return client


class TestAdminAuth:
    async def test_login_rejects_wrong_password(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/admin/login",
            data={"password": "wrong", "next": "/admin/"},
        )
        assert resp.status_code == 401  # noqa: PLR2004

    async def test_login_accepts_correct_password(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/admin/login",
            data={"password": ADMIN_PASSWORD, "next": "/admin/"},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # noqa: PLR2004

    async def test_dashboard_requires_auth(
        self, client: AsyncClient
    ) -> None:
        # require_admin_redirect returns a 303 to /admin/login for HTML routes
        resp = await client.get(
            "/admin/", follow_redirects=False
        )
        assert resp.status_code == 303  # noqa: PLR2004
        assert "/admin/login" in resp.headers.get("location", "")

    async def test_dashboard_works_when_logged_in(
        self, logged_in_client: AsyncClient
    ) -> None:
        resp = await logged_in_client.get("/admin/")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "Dashboard" in resp.text or "dashboard" in resp.text.lower()

    async def test_login_form_renders(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/admin/login")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "password" in resp.text.lower()

    async def test_already_logged_in_redirects_from_login(
        self, logged_in_client: AsyncClient
    ) -> None:
        resp = await logged_in_client.get(
            "/admin/login", follow_redirects=False
        )
        assert resp.status_code == 303  # noqa: PLR2004

    async def test_admin_disabled_when_no_password_hash(
        self,
        pool: asyncpg.pool.Pool,
    ) -> None:
        from archiver.blocklist import DomainBlocklist

        settings = Settings(
            db_url=DB_URL,
            admin_password_hash="",  # type: ignore[arg-type]
            session_secret="test-secret-xxxxxxxxxxxxxxxxxxxxxxxx",  # noqa: S106  # type: ignore[arg-type]
        )
        app = create_app(settings)
        app.state.pool = pool
        app.state.blocklist = DomainBlocklist()
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            resp = await c.get("/admin/login")
            assert resp.status_code == 404  # noqa: PLR2004

    async def test_logout_clears_session(
        self, logged_in_client: AsyncClient
    ) -> None:
        await logged_in_client.post(
            "/admin/logout", follow_redirects=False
        )
        resp = await logged_in_client.get(
            "/admin/", follow_redirects=False
        )
        # Redirected to login
        assert resp.status_code == 303  # noqa: PLR2004


class TestCaptchaIntegration:
    """Tests for captcha-protected report submissions."""

    async def _create_archive(self, client: AsyncClient) -> str:
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        return resp.json()["id"]

    async def test_altcha_challenge_requires_config(
        self, client: AsyncClient
    ) -> None:
        # Default captcha_provider=none; altcha endpoint should 404
        resp = await client.get("/captcha/altcha/challenge")
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_altcha_challenge_endpoint(
        self,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """When altcha enabled, challenge endpoint returns a signed challenge."""
        from archiver.blocklist import DomainBlocklist

        settings = Settings(
            db_url=DB_URL,
            captcha_provider="altcha",  # type: ignore[arg-type]
            altcha_hmac_key="test-hmac-key-at-least-32-bytes-123",  # type: ignore[arg-type]
        )
        app = create_app(settings)
        app.state.pool = pool
        app.state.blocklist = DomainBlocklist()
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            resp = await c.get("/captcha/altcha/challenge")
            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data["algorithm"] == "SHA-256"
            assert "challenge" in data
            assert "signature" in data


class TestReportWorkflow:
    async def _create_archive(self, client: AsyncClient) -> str:
        """Helper: create an archive and return its id."""
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        assert resp.status_code == 201  # noqa: PLR2004
        return resp.json()["id"]

    async def test_public_can_view_report_form(
        self, client: AsyncClient
    ) -> None:
        archive_id = await self._create_archive(client)
        resp = await client.get(f"/report/{archive_id}")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "Report" in resp.text

    async def test_public_can_submit_report(
        self, client: AsyncClient
    ) -> None:
        archive_id = await self._create_archive(client)
        resp = await client.post(
            f"/report/{archive_id}",
            data={
                "reason": "copyright",
                "details": "DMCA test",
                "reporter_email": "",
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert "received" in resp.text.lower() or "thank" in resp.text.lower()

    async def test_admin_sees_pending_reports(
        self, client: AsyncClient, logged_in_client: AsyncClient
    ) -> None:
        archive_id = await self._create_archive(client)
        await client.post(
            f"/report/{archive_id}",
            data={"reason": "malicious", "details": "test"},
        )
        resp = await logged_in_client.get(
            "/admin/reports?status=pending"
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert "malicious" in resp.text

    async def test_takedown_soft_deletes_archive(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        archive_id = await self._create_archive(client)
        # Submit report
        await client.post(
            f"/report/{archive_id}",
            data={"reason": "copyright", "details": "test"},
        )
        # Find report id
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM reports WHERE archive_id = $1",
                archive_id,
            )
            assert row is not None
            report_id = row["id"]
        # Take down
        resp = await logged_in_client.post(
            f"/admin/reports/{report_id}/resolve",
            data={"action": "resolve", "notes": "Valid"},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # noqa: PLR2004
        # Archive should be soft-deleted
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT removed_at FROM archives WHERE id = $1",
                archive_id,
            )
            assert row is not None
            assert row["removed_at"] is not None

    async def test_audit_log_records_takedown(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        archive_id = await self._create_archive(client)
        await client.post(
            f"/report/{archive_id}",
            data={"reason": "malicious"},
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM reports WHERE archive_id = $1",
                archive_id,
            )
            assert row is not None
            report_id = row["id"]
        await logged_in_client.post(
            f"/admin/reports/{report_id}/resolve",
            data={"action": "resolve"},
            follow_redirects=False,
        )
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE archive_id = $1",
                archive_id,
            )
            assert count >= 2  # soft_delete + report_resolved  # noqa: PLR2004

    async def test_ip_never_stored_raw(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """Verify reporter_ip_hash is a hash, not a raw IP."""
        archive_id = await self._create_archive(client)
        await client.post(
            f"/report/{archive_id}",
            data={"reason": "other", "details": "test"},
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT reporter_ip_hash FROM reports"
                " WHERE archive_id = $1",
                archive_id,
            )
            assert row is not None
            ip_hash = row["reporter_ip_hash"] or ""
            # Should be a hex hash (32 chars), not an IP (contains dots)
            assert "." not in ip_hash
            if ip_hash:
                assert all(c in "0123456789abcdef" for c in ip_hash)


class TestAdminArchiveManagement:
    async def test_admin_can_restore_soft_deleted(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        archive_id = resp.json()["id"]
        # Soft-delete via admin
        await logged_in_client.post(
            f"/admin/archives/{archive_id}/remove",
            data={"reason": "test"},
            follow_redirects=False,
        )
        # Restore
        await logged_in_client.post(
            f"/admin/archives/{archive_id}/restore",
            follow_redirects=False,
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT removed_at FROM archives WHERE id = $1",
                archive_id,
            )
            assert row is not None
            assert row["removed_at"] is None

    async def test_audit_log_viewer(
        self, logged_in_client: AsyncClient
    ) -> None:
        resp = await logged_in_client.get("/admin/audit")
        assert resp.status_code == 200  # noqa: PLR2004
        # Login action should be in the log
        assert "admin_login" in resp.text

    async def test_archives_list(
        self, client: AsyncClient, logged_in_client: AsyncClient
    ) -> None:
        await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        resp = await logged_in_client.get("/admin/archives")
        assert resp.status_code == 200  # noqa: PLR2004
        assert "example.com" in resp.text

    async def test_hard_delete_removes_record(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        archive_id = resp.json()["id"]
        await logged_in_client.post(
            f"/admin/archives/{archive_id}/hard-delete",
            follow_redirects=False,
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM archives WHERE id = $1", archive_id
            )
            assert row is None

    async def test_hard_delete_removes_artifact_dir(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
        pool: asyncpg.pool.Pool,
        tmp_path: Path,  # type: ignore[name-defined]
    ) -> None:
        """Hard-delete removes artifacts from disk when present."""
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        archive_id = resp.json()["id"]
        # Place fake artifacts on disk and update the archive record
        artifacts_dir = logged_in_client._transport.app.state.settings.artifacts_dir  # type: ignore[union-attr]
        rel_dir = f"hash/{archive_id[:8]}"
        artifact_path = artifacts_dir / rel_dir
        artifact_path.mkdir(parents=True, exist_ok=True)
        (artifact_path / "snapshot.html").write_bytes(b"test")

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE archives SET artifact_dir=$1 WHERE id=$2",
                rel_dir, archive_id,
            )
        await logged_in_client.post(
            f"/admin/archives/{archive_id}/hard-delete",
            follow_redirects=False,
        )
        # Artifact dir removed
        assert not artifact_path.exists()

    async def test_blocklist_reload(
        self, logged_in_client: AsyncClient
    ) -> None:
        resp = await logged_in_client.post(
            "/admin/blocklist/reload", follow_redirects=False
        )
        assert resp.status_code == 303  # noqa: PLR2004

    async def test_report_on_missing_archive(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/report/nonexistent",
            data={"reason": "other"},
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_report_form_for_missing_archive(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/report/nonexistent")
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_invalid_reason_rejected(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        archive_id = resp.json()["id"]
        resp = await client.post(
            f"/report/{archive_id}",
            data={"reason": "invalid-reason"},
        )
        assert resp.status_code == 400  # noqa: PLR2004

    async def test_resolve_missing_report(
        self, logged_in_client: AsyncClient
    ) -> None:
        resp = await logged_in_client.post(
            "/admin/reports/nonexistent/resolve",
            data={"action": "resolve"},
            follow_redirects=False,
        )
        assert resp.status_code == 404  # noqa: PLR2004

    async def test_invalid_status_filter_defaults_to_pending(
        self, logged_in_client: AsyncClient
    ) -> None:
        # Invalid status value should fall back to pending
        resp = await logged_in_client.get(
            "/admin/reports?status=garbage"
        )
        assert resp.status_code == 200  # noqa: PLR2004

    async def test_all_reports_filter(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
    ) -> None:
        # Create a report so the "all" filter has data
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        archive_id = resp.json()["id"]
        await client.post(
            f"/report/{archive_id}",
            data={"reason": "other"},
        )
        resp = await logged_in_client.get(
            "/admin/reports?status=all"
        )
        assert resp.status_code == 200  # noqa: PLR2004

    async def test_dismiss_report(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/", "force": True},
        )
        archive_id = resp.json()["id"]
        await client.post(
            f"/report/{archive_id}",
            data={"reason": "other", "details": "test dismiss"},
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM reports WHERE archive_id = $1",
                archive_id,
            )
            assert row is not None
            report_id = row["id"]
        await logged_in_client.post(
            f"/admin/reports/{report_id}/resolve",
            data={"action": "dismiss", "notes": "not abuse"},
            follow_redirects=False,
        )
        # Archive should NOT be removed
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT removed_at FROM archives WHERE id = $1",
                archive_id,
            )
            assert row is not None
            assert row["removed_at"] is None
