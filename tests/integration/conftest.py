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
