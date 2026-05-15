# ABOUTME: Shared integration-test guards — refuse to run against non-test DBs
# ABOUTME: Reads ARCHIVER_TEST_DB_URL; skips the whole package if the DB looks production-y
"""Integration-test conftest.

Every integration test in this directory points at a Postgres DB,
and most of them mutate it destructively at teardown — DELETE FROM
on a half-dozen tables (test_api, test_pages, test_admin,
test_repository) and DROP TABLE CASCADE on archives + jobs
(test_db). A stray ``pytest tests/integration/`` from a dev shell
pointed at the dev or prod DB therefore wipes data and tables.

The guard below refuses to load this package unless ``ARCHIVER_TEST_DB_URL``
points at a DB whose name ends in one of the safe suffixes
(``_test`` / ``_ci`` / ``_tmp``). CI / local "make test-integration"
provisions ``archiver_test``; running without setting the env var
results in a clean skip rather than a silent wipe.

This complements (and supersedes) the per-file guard that previously
lived only in ``test_repository.py`` — now every file in the
package picks it up automatically.
"""

from __future__ import annotations

import os

import pytest

DB_URL = os.environ.get(
    "ARCHIVER_TEST_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver_test",
)

_TEST_DB_SUFFIXES = ("_test", "_ci", "_tmp")


def _is_safe_test_db(url: str) -> bool:
    path = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return any(path.endswith(s) for s in _TEST_DB_SUFFIXES)


if not _is_safe_test_db(DB_URL):
    pytest.skip(
        f"refusing to run integration tests against {DB_URL!r}: "
        f"these tests wipe / drop tables on teardown, so the database "
        f"name must end in one of {_TEST_DB_SUFFIXES} to opt in. Set "
        f"ARCHIVER_TEST_DB_URL to a test database (e.g. "
        f"postgresql://.../archiver_test).",
        allow_module_level=True,
    )


# Tables every integration-test fixture should wipe in setup so that
# random-order test runs are deterministic. Listed in safe deletion
# order (children before parents) so FK cascades don't matter. The
# operational tables that the live worker repopulates from external
# state (proxy_status, frontend_status, domain_observations,
# cf_clearance_cache) are included so e.g. a test that asserts on
# a specific proxy doesn't inherit rows from an earlier file.
#
# `schema_migrations` is deliberately NOT wiped — it's the migration
# tracker. Wiping it would re-run 001 on the next init_db call,
# which is wasted work since 001 is already applied to the test DB.
_RESET_TABLES = (
    "audit_log",
    "reports",
    "jobs",
    "archives",
    "proxy_status",
    "frontend_status",
    "domain_observations",
    "cf_clearance_cache",
)


async def reset_test_db(pool) -> None:  # type: ignore[no-untyped-def]
    """Truncate every mutable table on the test DB.

    Use in test fixtures' SETUP (not teardown) — clean state on the
    way IN guarantees each test starts deterministic regardless of
    what ran before it. Teardown-only cleanup leaks state when a
    prior test crashes mid-fixture, when pytest-randomly orders a
    fixture that wipes table A before one that asserts on table B,
    or when a teardown's table list doesn't match what setup creates.

    TRUNCATE … CASCADE is faster than per-row DELETE on populated
    tables and removes FK-cascade ordering concerns. Wrapped in a
    single transaction so partial failures don't leave the DB
    half-clean.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            f"TRUNCATE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE"
        )
