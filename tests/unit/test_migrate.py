# ABOUTME: Unit tests for the migration runner — list + apply, idempotency
# ABOUTME: Uses tmp_path fixtures and asyncpg mocks; integration tests cover live DB
"""Tests for archiver.migrate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg

from archiver.migrate import _list_migrations


class TestListMigrations:
    def test_returns_empty_for_missing_directory(self, tmp_path: Path) -> None:
        missing = tmp_path / "no_such_dir"
        assert _list_migrations(missing) == []

    def test_sorts_by_filename(self, tmp_path: Path) -> None:
        # Create out of natural order to prove sorting is by name, not mtime
        (tmp_path / "003_third.sql").write_text("-- 3")
        (tmp_path / "001_first.sql").write_text("-- 1")
        (tmp_path / "002_second.sql").write_text("-- 2")
        result = _list_migrations(tmp_path)
        versions = [v for v, _ in result]
        assert versions == ["001_first", "002_second", "003_third"]

    def test_ignores_non_sql_files(self, tmp_path: Path) -> None:
        """README, __init__.py, .gitkeep etc. must not pollute the list."""
        (tmp_path / "001_real.sql").write_text("-- 1")
        (tmp_path / "README.md").write_text("# notes")
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / ".gitkeep").write_text("")
        result = _list_migrations(tmp_path)
        assert [v for v, _ in result] == ["001_real"]

    def test_uses_filename_stem_as_version(self, tmp_path: Path) -> None:
        (tmp_path / "042_add_foo_table.sql").write_text("-- foo")
        result = _list_migrations(tmp_path)
        assert result[0][0] == "042_add_foo_table"

    def test_skips_subdirectories(self, tmp_path: Path) -> None:
        """Only top-level .sql files count; nested dirs are organisational."""
        (tmp_path / "001_real.sql").write_text("-- 1")
        sub = tmp_path / "archived"
        sub.mkdir()
        (sub / "999_dead.sql").write_text("-- never runs")
        result = _list_migrations(tmp_path)
        assert [v for v, _ in result] == ["001_real"]


class TestMigrateApply:
    """The async migrate() function with mocked asyncpg pool."""

    @staticmethod
    def _mock_pool_and_conn() -> tuple[MagicMock, AsyncMock]:
        """Pool whose acquire() context returns an async-mock connection.

        The pool MagicMock uses spec=asyncpg.Pool so beartype's runtime
        isinstance check passes — without that, the @beartype decorator
        on migrate() raises BeartypeCallHintParamViolation.
        """
        conn = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock(spec=asyncpg.Pool)
        pool.acquire.return_value = ctx
        # transaction() returns an async ctx manager synchronously
        # (no await). AsyncMock would coroutine-wrap the return,
        # which doesn't support `async with`. Use a plain MagicMock
        # that returns an AsyncMock context.
        tx_ctx = AsyncMock()
        tx_ctx.__aenter__ = AsyncMock()
        tx_ctx.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx_ctx)
        return pool, conn

    async def test_no_pending_returns_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """All migrations on disk are already in schema_migrations -> []."""
        from archiver import migrate as migrate_mod

        (tmp_path / "001_initial.sql").write_text("SELECT 1;")
        monkeypatch.setattr(migrate_mod, "_MIGRATIONS_DIR", tmp_path)

        pool, conn = self._mock_pool_and_conn()
        # fetch returns a list of row-likes with .__getitem__ via dict
        conn.fetch = AsyncMock(return_value=[{"version": "001_initial"}])

        applied = await migrate_mod.migrate(pool)
        assert applied == []
        # The tracking-table-create should have been called once but
        # no per-migration INSERT statements.
        execute_calls = [c.args[0] for c in conn.execute.await_args_list]
        # First call creates schema_migrations table; no INSERT for 001
        assert any("schema_migrations" in c for c in execute_calls)
        assert not any("INSERT INTO schema_migrations" in c for c in execute_calls)

    async def test_applies_pending_in_order(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Two pending migrations get executed + recorded in order."""
        from archiver import migrate as migrate_mod

        (tmp_path / "001_a.sql").write_text("-- one")
        (tmp_path / "002_b.sql").write_text("-- two")
        monkeypatch.setattr(migrate_mod, "_MIGRATIONS_DIR", tmp_path)

        pool, conn = self._mock_pool_and_conn()
        conn.fetch = AsyncMock(return_value=[])  # nothing applied yet

        applied = await migrate_mod.migrate(pool)
        assert applied == ["001_a", "002_b"]

        # Inspect the recorded INSERTs to confirm both versions landed
        insert_args = [
            c for c in conn.execute.await_args_list
            if "INSERT INTO schema_migrations" in c.args[0]
        ]
        recorded_versions = [c.args[1] for c in insert_args]
        assert recorded_versions == ["001_a", "002_b"]

    async def test_skips_already_applied(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Only the missing migration runs when 001 is in the table."""
        from archiver import migrate as migrate_mod

        (tmp_path / "001_a.sql").write_text("-- one")
        (tmp_path / "002_b.sql").write_text("-- two")
        monkeypatch.setattr(migrate_mod, "_MIGRATIONS_DIR", tmp_path)

        pool, conn = self._mock_pool_and_conn()
        conn.fetch = AsyncMock(return_value=[{"version": "001_a"}])

        applied = await migrate_mod.migrate(pool)
        assert applied == ["002_b"]

        # Confirm 001 was NOT re-executed (its SQL would have appeared
        # in an execute call between the tracking-table create and the
        # 002 INSERT)
        non_track_executes = [
            c.args[0] for c in conn.execute.await_args_list
            if "schema_migrations" not in c.args[0]
        ]
        assert any("-- two" in s for s in non_track_executes)
        assert not any("-- one" in s for s in non_track_executes)
