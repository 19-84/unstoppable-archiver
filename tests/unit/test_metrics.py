# ABOUTME: Tests for Prometheus metrics registry wiring
# ABOUTME: Sanity-checks counters/gauges exist and serialize to text format
"""Tests for metrics module."""

from __future__ import annotations

from archiver.metrics import (
    blocklist_hits_total,
    captures_total,
    jobs_queued,
    prometheus_text,
    rate_limit_exceeded_total,
)


class TestMetrics:
    def test_prometheus_text_is_bytes(self) -> None:
        body, content_type = prometheus_text()
        assert isinstance(body, bytes)
        assert "text/plain" in content_type

    def test_captures_counter_increments(self) -> None:
        # Use private API to read counter value
        before = captures_total.labels(tier="chromium", outcome="complete")._value.get()
        captures_total.labels(tier="chromium", outcome="complete").inc()
        after = captures_total.labels(tier="chromium", outcome="complete")._value.get()
        assert after == before + 1

    def test_blocklist_counter_exists(self) -> None:
        blocklist_hits_total.inc()
        body, _ = prometheus_text()
        assert b"archiver_blocklist_hits_total" in body

    def test_rate_limit_label_works(self) -> None:
        rate_limit_exceeded_total.labels(endpoint="/test").inc()
        body, _ = prometheus_text()
        assert b"archiver_rate_limit_exceeded_total" in body

    def test_jobs_queued_gauge(self) -> None:
        jobs_queued.set(42)
        body, _ = prometheus_text()
        assert b"archiver_jobs_queued 42.0" in body
