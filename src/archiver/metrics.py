# ABOUTME: Prometheus metrics for operational observability
# ABOUTME: Counters for captures/blocklist/rate-limit; gauges for queue depth
"""Prometheus metrics definitions."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Capture lifecycle
captures_total = Counter(
    "archiver_captures_total",
    "Total capture attempts by tier and outcome",
    ["tier", "outcome"],  # outcome: complete, failed, antibot
)

capture_duration_seconds = Histogram(
    "archiver_capture_duration_seconds",
    "Time spent in capture_page() per tier",
    ["tier"],
    buckets=(1, 5, 10, 30, 60, 120, 300),
)

# Queue
jobs_queued = Gauge(
    "archiver_jobs_queued",
    "Current number of queued jobs",
)

jobs_running = Gauge(
    "archiver_jobs_running",
    "Currently running jobs",
)

# Abuse signals
blocklist_hits_total = Counter(
    "archiver_blocklist_hits_total",
    "Domain blocklist rejections",
)

rate_limit_exceeded_total = Counter(
    "archiver_rate_limit_exceeded_total",
    "Requests rejected by rate limiter",
    ["endpoint"],
)

# Admin
admin_logins_total = Counter(
    "archiver_admin_logins_total",
    "Admin login attempts by outcome",
    ["outcome"],  # success, failure
)

reports_total = Counter(
    "archiver_reports_total",
    "Abuse reports filed by reason",
    ["reason"],
)


def prometheus_text() -> tuple[bytes, str]:
    """Return (metrics_text, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
