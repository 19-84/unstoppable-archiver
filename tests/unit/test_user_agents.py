# ABOUTME: Tests for the rotating User-Agent pool + daily refresh
# ABOUTME: Verifies pool loads, pick() randomizes, refresh handles errors + cache
"""Tests for archiver.user_agents."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from archiver import user_agents


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path: Path) -> object:  # type: ignore[misc]
    """Isolate each test — reset in-memory pool and point cache at tmp."""
    orig_pool = list(user_agents._pool)
    orig_ts = user_agents._last_refresh_ts
    orig_cache = user_agents._CACHE_PATH
    user_agents._pool = list(user_agents._BUNDLED_POOL)
    user_agents._last_refresh_ts = 0.0
    user_agents._CACHE_PATH = tmp_path / "uas.json"
    yield
    user_agents._pool = orig_pool
    user_agents._last_refresh_ts = orig_ts
    user_agents._CACHE_PATH = orig_cache


class TestPick:
    def test_returns_string_from_bundled_pool(self) -> None:
        ua = user_agents.pick()
        assert isinstance(ua, str)
        assert ua in user_agents._BUNDLED_POOL

    def test_pick_randomizes(self) -> None:
        # Over many calls we should see more than one distinct UA.
        seen = {user_agents.pick() for _ in range(50)}
        assert len(seen) > 1

    def test_never_leaks_archiver_identity(self) -> None:
        """Hard invariant: project name must never appear in any UA."""
        banned = ("archiver", "unstoppable", "bot", "crawl", "python-httpx")
        for ua in user_agents._BUNDLED_POOL:
            lower = ua.lower()
            for term in banned:
                assert term not in lower, f"banned token {term!r} in {ua!r}"


class TestRefresh:
    @respx.mock
    async def test_fetches_remote_and_updates_pool(self) -> None:
        fake_uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/999.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/20.0 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64; rv:999.0) Gecko/20100101 Firefox/999.0",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 99_0) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/999.0.0.0 Edg/999.0.0.0",
        ]
        respx.get(user_agents._SOURCE_URL).mock(
            return_value=httpx.Response(200, json=fake_uas)
        )
        size = await user_agents.refresh(force=True)
        assert size == len(fake_uas)
        # pick() should now return one of the fetched UAs
        picked = {user_agents.pick() for _ in range(30)}
        assert picked.issubset(set(fake_uas))

    @respx.mock
    async def test_network_failure_keeps_bundled_pool(self) -> None:
        respx.get(user_agents._SOURCE_URL).mock(
            side_effect=httpx.ConnectError("boom")
        )
        await user_agents.refresh(force=True)
        # Pool should still contain only bundled entries (network failed,
        # cache empty).
        assert set(user_agents._pool) == set(user_agents._BUNDLED_POOL)

    @respx.mock
    async def test_invalid_response_falls_back(self) -> None:
        """A 200 response with garbage JSON shouldn't corrupt the pool."""
        respx.get(user_agents._SOURCE_URL).mock(
            return_value=httpx.Response(200, json={"not": "a list"})
        )
        await user_agents.refresh(force=True)
        assert set(user_agents._pool) == set(user_agents._BUNDLED_POOL)

    @respx.mock
    async def test_loads_from_fresh_cache(self, tmp_path: Path) -> None:
        """If cache is fresh, we don't hit the network."""
        cached = [
            "Mozilla/5.0 (cached) AppleWebKit/537.36 Chrome/100.0.0.0 Safari/537.36 long_enough",
            "Mozilla/5.0 (cached2) AppleWebKit/537.36 Chrome/100.0.0.0 Safari/537.36 padding",
        ]
        user_agents._CACHE_PATH.write_text(json.dumps(cached))
        # Route not mocked — any HTTP call would raise
        size = await user_agents.refresh(force=True)
        assert size == len(cached)
        assert set(user_agents._pool) == set(cached)

    @respx.mock
    async def test_staleness_gate_prevents_refetch(self) -> None:
        """Two back-to-back refresh()s trigger only one HTTP call."""
        call_count = {"n": 0}
        fake_uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/999.0.0.0 Safari/537.36",
        ] * 5

        def responder(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json=fake_uas)

        respx.get(user_agents._SOURCE_URL).mock(side_effect=responder)
        await user_agents.refresh(force=True)
        assert call_count["n"] == 1
        # Second call, no force: should short-circuit
        await user_agents.refresh()
        assert call_count["n"] == 1


class TestPoolEdges:
    def test_current_pool_size_reports_len(self) -> None:
        user_agents._pool = ["a" * 40, "b" * 40, "c" * 40]
        assert user_agents.current_pool_size() == 3  # noqa: PLR2004

    @respx.mock
    async def test_stale_cache_is_ignored(self, tmp_path: Path) -> None:
        """Cache older than TTL should be skipped and network used."""
        import os

        cached = ["Mozilla/5.0 (old) AppleWebKit/537.36 Chrome/100.0.0.0 Safari/537.36 pad"]
        user_agents._CACHE_PATH.write_text(json.dumps(cached))
        # Backdate the cache file past TTL
        old = 1.0
        os.utime(user_agents._CACHE_PATH, (old, old))

        fresh = [
            "Mozilla/5.0 (new) AppleWebKit/537.36 Chrome/200.0.0.0 Safari/537.36 pad",
            "Mozilla/5.0 (new2) AppleWebKit/537.36 Chrome/200.0.0.0 Safari/537.36 pad",
        ]
        respx.get(user_agents._SOURCE_URL).mock(
            return_value=httpx.Response(200, json=fresh)
        )
        await user_agents.refresh(force=True)
        assert set(user_agents._pool) == set(fresh)

    @respx.mock
    async def test_cache_with_invalid_shape_ignored(self) -> None:
        """Fresh cache containing a non-list falls through to network."""
        user_agents._CACHE_PATH.write_text(json.dumps({"bad": "shape"}))
        fresh = [
            "Mozilla/5.0 (ok) AppleWebKit/537.36 Chrome/200.0.0.0 Safari/537.36 pad",
        ]
        respx.get(user_agents._SOURCE_URL).mock(
            return_value=httpx.Response(200, json=fresh)
        )
        await user_agents.refresh(force=True)
        assert set(user_agents._pool) == set(fresh)

    @respx.mock
    async def test_cache_read_exception_falls_through(self) -> None:
        """A broken cache file (non-JSON) should be swallowed, not crash."""
        user_agents._CACHE_PATH.write_text("{not valid json")
        fresh = [
            "Mozilla/5.0 (ok) AppleWebKit/537.36 Chrome/200.0.0.0 Safari/537.36 pad",
        ]
        respx.get(user_agents._SOURCE_URL).mock(
            return_value=httpx.Response(200, json=fresh)
        )
        size = await user_agents.refresh(force=True)
        assert size == len(fresh)

    @respx.mock
    async def test_cache_write_exception_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache-write IO error must not break a successful refresh."""
        fresh = [
            "Mozilla/5.0 (ok) AppleWebKit/537.36 Chrome/200.0.0.0 Safari/537.36 pad",
        ]
        respx.get(user_agents._SOURCE_URL).mock(
            return_value=httpx.Response(200, json=fresh)
        )

        orig_write = Path.write_text

        def broken_write(self: Path, *args: object, **kwargs: object) -> int:
            if self == user_agents._CACHE_PATH:
                raise OSError("disk full")
            return orig_write(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", broken_write)
        size = await user_agents.refresh(force=True)
        assert size == len(fresh)
        assert set(user_agents._pool) == set(fresh)

    @respx.mock
    async def test_empty_pool_falls_back_to_bundled(self) -> None:
        """If every source fails AND pool is empty, bundled is restored."""
        user_agents._pool = []
        respx.get(user_agents._SOURCE_URL).mock(
            side_effect=httpx.ConnectError("boom")
        )
        size = await user_agents.refresh(force=True)
        assert size == len(user_agents._BUNDLED_POOL)
        assert set(user_agents._pool) == set(user_agents._BUNDLED_POOL)


class TestPoolValidation:
    def test_rejects_short_strings(self) -> None:
        assert user_agents._is_valid_pool(["short"]) is False

    def test_rejects_non_list(self) -> None:
        assert user_agents._is_valid_pool({"k": "v"}) is False
        assert user_agents._is_valid_pool(None) is False

    def test_rejects_empty_list(self) -> None:
        assert user_agents._is_valid_pool([]) is False

    def test_accepts_valid_uas(self) -> None:
        good = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ]
        assert user_agents._is_valid_pool(good) is True
