# ABOUTME: Shared pytest fixtures for all test suites
# ABOUTME: Provides database connections, test servers, and common helpers
"""Shared test fixtures for unstoppable-archive."""

from __future__ import annotations

from hypothesis import HealthCheck, settings

# Property tests run in CI on shared, variably-loaded runners. Two
# Hypothesis defaults are wall-clock based and therefore flaky under
# load — independent of any logic bug:
#   * `deadline` fails an example that takes too long to *execute*
#   * HealthCheck.too_slow fails when input *generation* is slow
# Both surface as intermittent FailedHealthCheck/DeadlineExceeded on a
# busy box, with a seemingly random property test as the victim. Disable
# the timing checks globally (correctness assertions are unaffected) so
# CI stays deterministic. Registered as the default profile so every
# @given/@settings test inherits it.
settings.register_profile(
    "default",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("default")
