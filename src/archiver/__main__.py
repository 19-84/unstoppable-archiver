# ABOUTME: Entry point for the worker process via python -m archiver
# ABOUTME: Sets up logging, signal handlers, and runs the Worker main loop
"""Worker entry point: python -m archiver."""

from __future__ import annotations

import asyncio
import signal

import structlog
from beartype import beartype
from prometheus_client import start_http_server

from archiver.config import Settings
from archiver.logging import setup_logging
from archiver.worker import Worker

log = structlog.get_logger()


@beartype
def main() -> None:
    """Start the worker process."""
    settings = Settings()
    setup_logging(settings.log_level, settings.log_format)

    # Expose Prometheus metrics on a dedicated port so captures_total,
    # capture_duration_seconds etc. (which live in the worker process,
    # not the API process) are scrapeable. 0 disables.
    if settings.worker_metrics_port > 0:
        start_http_server(settings.worker_metrics_port)
        log.info(
            "worker.metrics_server_started",
            port=settings.worker_metrics_port,
        )

    worker = Worker(settings)
    loop = asyncio.new_event_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.ensure_future(worker.shutdown()),
        )

    try:
        loop.run_until_complete(worker.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
