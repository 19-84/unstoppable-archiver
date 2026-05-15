# ABOUTME: Versioned SQL migration runner using asyncpg
# ABOUTME: Applies files from src/archiver/migrations/ in numeric order; tracks state in schema_migrations
"""Migration runner.

Replaces the previous ad-hoc init_db pattern (one big SCHEMA_SQL
string of CREATE-IF-NOT-EXISTS + ALTER-IF-NOT-EXISTS) with a
file-based versioned scheme:

  src/archiver/migrations/
    001_initial_schema.sql   <- current full schema (frozen)
    002_<description>.sql    <- next change
    ...

A `schema_migrations` table tracks which versions have been applied.
At startup we read the migrations directory, compare against the
table, and execute the missing files in order. The whole pass runs
inside a transaction per file, so a failed migration leaves the DB
in the last-good state instead of half-applied.

**Why hand-rolled instead of Alembic** — the project uses asyncpg
directly with no SQLAlchemy. Alembic operates either on top of
SQLAlchemy models or in --offline SQL mode, both awkward fits. The
runtime semantics here are ~80 LOC and don't carry an extra deps
budget. New schema changes just add a numbered .sql file; nothing
to learn beyond `BEGIN; ... COMMIT;`.

**Existing-DB compatibility** — 001 is the current schema with all
its IF-NOT-EXISTS / DO-block guards intact. Applied to a dev DB
that was already up-to-date via the old init_db path, every
statement is a no-op; the runner just records 001 as applied so
002+ sequence correctly.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import structlog
from beartype import beartype

log = structlog.get_logger()

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@beartype
def _list_migrations(migrations_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return [(version, path)] sorted by filename.

    Migration files follow `NNN_<description>.sql`. The `NNN_` prefix
    is the version key recorded in schema_migrations; sorting by it
    gives deterministic apply order. Anything else in the directory
    (README, .gitkeep, sibling .py files) is ignored.

    `migrations_dir=None` resolves the module-level _MIGRATIONS_DIR
    at call time, so tests can monkeypatch it. A function default of
    _MIGRATIONS_DIR would be bound at import time and ignore later
    reassignments.
    """
    if migrations_dir is None:
        migrations_dir = _MIGRATIONS_DIR
    if not migrations_dir.exists():
        return []
    files = sorted(
        p for p in migrations_dir.iterdir()
        if p.is_file() and p.suffix == ".sql"
    )
    return [(p.stem, p) for p in files]


@beartype
async def migrate(pool: asyncpg.Pool) -> list[str]:
    """Apply any pending migrations. Returns the list of versions just applied.

    Each file runs in its own transaction — a failure aborts that
    file's changes but leaves earlier files committed. The
    tracking-table insert is part of the same transaction as the
    migration, so a row never appears for a partial apply.
    """
    async with pool.acquire() as conn:
        await conn.execute(_TRACKING_TABLE_SQL)
        applied: set[str] = {
            r["version"]
            for r in await conn.fetch("SELECT version FROM schema_migrations")
        }

    migrations = _list_migrations()
    pending = [(v, p) for v, p in migrations if v not in applied]

    if not pending:
        log.info(
            "db.migrate.up_to_date",
            applied_count=len(applied),
            available=len(migrations),
        )
        return []

    log.info(
        "db.migrate.applying",
        pending=[v for v, _ in pending],
        already_applied=len(applied),
    )

    applied_now: list[str] = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1)",
                version,
            )
        applied_now.append(version)
        log.info("db.migrate.applied", version=version)

    return applied_now
