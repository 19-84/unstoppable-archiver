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


class TestAdminLoginRateLimit:
    """Brute-force protection on /admin/login. bcrypt cost slows
    attempts to ~10/sec but doesn't prevent enumeration of a weak
    password over hours — without a per-IP cap, an attacker can
    grind through ~36k attempts/hour. The limit applies to all
    POSTs to /admin/login (success + fail). Legitimate operators
    don't re-login often enough to hit it; once authenticated, the
    session lasts 14 days."""

    async def test_login_rate_limit_caps_attempts(
        self,
        pool: asyncpg.pool.Pool,
    ) -> None:
        from httpx import ASGITransport

        from archiver.blocklist import DomainBlocklist
        from archiver.rate_limit import _global_limiter
        # Reset the in-memory limiter so prior tests don't bleed in.
        _global_limiter._windows.clear()

        settings = Settings(
            db_url=DB_URL,
            log_format="console",
            admin_password_hash=hash_password(ADMIN_PASSWORD),  # type: ignore[arg-type]
            session_secret="test-rate-limit-secret-min-32-bytes-xxxx",  # noqa: S106  # type: ignore[arg-type]
            rate_limit_enabled=True,
            rate_limit_login_per_hour=3,
        )
        app = create_app(settings)
        app.state.pool = pool
        app.state.blocklist = DomainBlocklist()
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(
            transport=transport, base_url="http://test",
        ) as c:
            # First 3 wrong-password attempts return 401 (auth fail).
            for i in range(3):
                resp = await c.post(
                    "/admin/login",
                    data={"password": "wrong", "next": "/admin/"},
                )
                assert resp.status_code == 401, f"attempt {i}: {resp.status_code}"  # noqa: PLR2004
            # 4th attempt MUST be capped at 429 — bcrypt cost no
            # longer running because the rate limiter trips first.
            resp = await c.post(
                "/admin/login",
                data={"password": "wrong", "next": "/admin/"},
            )
            assert resp.status_code == 429  # noqa: PLR2004
            # Retry-After header surfaced so brute-forcer / friendly
            # error page knows when to come back.
            assert "Retry-After" in resp.headers
            # Even a CORRECT password is blocked while rate-limited —
            # this is the right tradeoff; legitimate operators re-auth
            # at most once per session-lifetime (14 days).
            resp = await c.post(
                "/admin/login",
                data={"password": ADMIN_PASSWORD, "next": "/admin/"},
            )
            assert resp.status_code == 429  # noqa: PLR2004


class TestAdminAuth:
    async def test_login_rejects_wrong_password(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/admin/login",
            data={"password": "wrong", "next": "/admin/"},
        )
        assert resp.status_code == 401  # noqa: PLR2004

    async def test_login_form_has_password_manager_and_a11y_hints(
        self, client: AsyncClient
    ) -> None:
        """The login form's password field must carry
        autocomplete="current-password" so password managers
        recognise and fill it. The GET form renders it; a failed
        POST re-renders with an error that must be announced via
        role="alert" and wired to the field via aria-describedby."""
        # GET the empty login form.
        resp = await client.get("/admin/login")
        assert resp.status_code == 200  # noqa: PLR2004
        assert 'autocomplete="current-password"' in resp.text

        # A failed login re-renders the form with the error block.
        resp = await client.post(
            "/admin/login",
            data={"password": "definitely-wrong", "next": "/admin/"},
        )
        body = resp.text
        assert 'autocomplete="current-password"' in body
        # The error is announced and associated with the input.
        assert 'role="alert"' in body
        assert 'id="login-error"' in body
        assert 'aria-describedby="login-error"' in body

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

    async def test_invalid_reason_re_renders_form_with_error(
        self, client: AsyncClient,
    ) -> None:
        """A POST with an unrecognized reason (typo / DevTools edit /
        direct curl) used to return a bare ``{"detail":"Invalid
        reason: spam"}`` JSON 400. Browser users hitting that path
        saw an unstyled JSON blob with no path back to fixing their
        input. The route now re-renders the report form with an
        inline error banner and the same Glass Noir styling."""
        archive_id = await self._create_archive(client)
        resp = await client.post(
            f"/report/{archive_id}",
            data={
                "reason": "spam-this-is-not-valid",
                "details": "any details",
            },
        )
        assert resp.status_code == 400  # noqa: PLR2004
        assert resp.headers["content-type"].startswith("text/html")
        body = resp.text
        # Form re-rendered (not a bare JSON blob)
        assert "Report this archive" in body
        # Error block surfaced as an alert
        assert 'role="alert"' in body
        # The bad reason value is echoed back so the user can see what
        # they sent (helps with DevTools/auto-fill debugging).
        assert "spam-this-is-not-valid" in body
        # Original action URL preserved so retry goes to the same endpoint
        assert f'action="/report/{archive_id}"' in body

    async def test_duplicate_report_from_same_ip_silent_dedup(
        self,
        client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """A second report from the same IP for the same archive must
        NOT create a new row — the migration-002 unique index +
        repository's UniqueViolationError handler return the existing
        row so the user sees normal-success but admins aren't spammed."""
        archive_id = await self._create_archive(client)
        for details in ("first attempt", "second attempt"):
            resp = await client.post(
                f"/report/{archive_id}",
                data={"reason": "malicious", "details": details},
            )
            assert resp.status_code == 200  # noqa: PLR2004
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM reports WHERE archive_id=$1",
                archive_id,
            )
            # Only the FIRST report is in the table; the duplicate
            # was silently dropped via the unique-index conflict.
            assert count == 1

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


class TestAdminPagesSmoke:
    """Render-smoke each admin GET page with realistic data.

    The existing TestAdmin* classes assert on specific behaviour
    (resolving reports, restoring archives). This class catches the
    cheaper regression: a template that references a removed field
    or filter and 500s for everyone the moment it ships. Tests run
    fast because they only POST one archive + one report before
    GETting each page.
    """

    async def _setup_fixtures(
        self, client: AsyncClient,
    ) -> str:
        """Create one archive + one report so every admin page has
        non-empty data to render."""
        resp = await client.post(
            "/api/archives",
            json={"url": "https://example.com/admin-smoke", "force": True},
        )
        archive_id = resp.json()["id"]
        await client.post(
            f"/report/{archive_id}",
            data={"reason": "malicious", "details": "smoke"},
        )
        return archive_id

    async def test_dashboard_renders(
        self, client: AsyncClient, logged_in_client: AsyncClient,
    ) -> None:
        await self._setup_fixtures(client)
        resp = await logged_in_client.get("/admin/")
        assert resp.status_code == 200  # noqa: PLR2004
        # Dashboard links to the three sub-pages
        assert "/admin/reports" in resp.text
        assert "/admin/archives" in resp.text
        assert "/admin/audit" in resp.text

    async def test_archives_page_renders(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
    ) -> None:
        archive_id = await self._setup_fixtures(client)
        resp = await logged_in_client.get("/admin/archives")
        assert resp.status_code == 200  # noqa: PLR2004
        assert archive_id in resp.text

    async def test_admin_views_show_source_icons_for_every_tier(
        self,
        logged_in_client: AsyncClient,
        pool: asyncpg.pool.Pool,
    ) -> None:
        """Admin moderation views must visually distinguish capture
        sources so the operator can tell at a glance whether each
        archive came from a fallback path (wayback / archive.today /
        privacy_frontend / commoncrawl) or a direct browser capture.
        Earlier these views showed URL + status + date with no source
        cue at all — moderators had to click through to the detail
        page to see provenance."""
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO archives (id, url, url_hash, status, source,
                    tier, created_at, completed_at, title, artifact_dir,
                    snapshot_size)
                VALUES
                  ($1, $6, $11, 'complete', 'direct', 'chromium',
                   now(), now(), 'D', 'admsrc/A', 100),
                  ($2, $7, $12, 'complete', 'wayback', 'wayback',
                   now(), now(), 'W', 'admsrc/B', 100),
                  ($3, $8, $13, 'complete', 'archive_today',
                   'archive_today', now(), now(), 'AT',
                   'admsrc/C', 100),
                  ($4, $9, $14, 'complete', 'privacy_frontend',
                   'privacy_frontend', now(), now(), 'PF',
                   'admsrc/D', 100),
                  ($5, $10, $15, 'complete', 'commoncrawl',
                   'commoncrawl', now(), now(), 'CC',
                   'admsrc/E', 100)
                """,
                "01TESTADMSRC100000000000",
                "01TESTADMSRC200000000000",
                "01TESTADMSRC300000000000",
                "01TESTADMSRC400000000000",
                "01TESTADMSRC500000000000",
                "https://example.com/admsrc-1",
                "https://example.com/admsrc-2",
                "https://example.com/admsrc-3",
                "https://example.com/admsrc-4",
                "https://example.com/admsrc-5",
                "admsrch-1-hash-32chars-abcdefghi",
                "admsrch-2-hash-32chars-jklmnopqr",
                "admsrch-3-hash-32chars-stuvwxyz1",
                "admsrch-4-hash-32chars-23456789a",
                "admsrch-5-hash-32chars-bcdefghij",
            )

        # Both moderation surfaces — dashboard recent list AND the
        # /admin/archives full list — must surface all five icons.
        for path in ("/admin/", "/admin/archives?limit=20"):
            resp = await logged_in_client.get(path)
            assert resp.status_code == 200  # noqa: PLR2004
            body = resp.text
            for label in ("direct", "wayback", "archive.today",
                          "privacy frontend", "common crawl"):
                assert f'title="{label}"' in body, (
                    f"admin path {path} missing source label {label}"
                )

    async def test_reports_page_renders_each_status_filter(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
    ) -> None:
        """All three status filter URLs must render — pending is the
        landing case but the other two are reachable from the nav."""
        await self._setup_fixtures(client)
        for status in ("pending", "resolved", "dismissed"):
            resp = await logged_in_client.get(
                f"/admin/reports?status={status}",
            )
            assert resp.status_code == 200, (  # noqa: PLR2004
                f"reports?status={status} returned {resp.status_code}"
            )

    async def test_audit_page_renders(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
    ) -> None:
        await self._setup_fixtures(client)
        # Trigger an audit-loggable action so the page has at least
        # one entry to render (otherwise we just verify the empty state)
        await logged_in_client.post(
            "/admin/blocklist/reload", follow_redirects=False,
        )
        resp = await logged_in_client.get("/admin/audit")
        assert resp.status_code == 200  # noqa: PLR2004

    async def test_audit_page_renders_details_and_pagination(
        self,
        client: AsyncClient,
        logged_in_client: AsyncClient,
    ) -> None:
        """The audit log must surface the per-action `details` dict
        (reason for a removal, notes from a report resolution, etc.)
        and offer prev/next navigation when more entries exist than
        fit on one page. Earlier the template hid `details` entirely,
        so moderators could see *what* happened but never *why*; and
        the log silently truncated at the most recent 100 with no way
        to walk back."""
        archive_id = await self._setup_fixtures(client)

        # Resolve the report with a takedown so the audit row carries
        # a non-trivial details dict (report_id + notes).
        reports_page = await logged_in_client.get("/admin/reports")
        # Extract the seeded report id from the page
        import re
        m = re.search(r'/admin/reports/(\d[A-Z0-9]+)/resolve', reports_page.text)
        assert m is not None, "no pending report on /admin/reports"
        report_id = m.group(1)
        await logged_in_client.post(
            f"/admin/reports/{report_id}/resolve",
            data={"action": "resolve", "notes": "audit-trail-test"},
            follow_redirects=False,
        )

        body = (await logged_in_client.get("/admin/audit?limit=50")).text
        # The takedown-reason notes from the resolve form must appear
        # somewhere in the rendered audit row — that's the whole point.
        assert "audit-trail-test" in body
        # archive_id is rendered as a clickable link to the detail page
        # (the takedown stub for soft-deleted rows).
        assert f'href="/archive/{archive_id}"' in body
        # Pagination nav must render with at least the 'Newer' or
        # 'Older' control gated on offset / fullness.
        assert 'aria-label="Audit pagination"' in body
        # offset=10 must not 500 — even if entries are sparse the
        # template should render an empty rows block + a Newer link.
        resp = await logged_in_client.get("/admin/audit?limit=10&offset=10")
        assert resp.status_code == 200  # noqa: PLR2004
