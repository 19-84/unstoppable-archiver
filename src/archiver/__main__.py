# ABOUTME: Entry point for the worker process via python -m archiver
# ABOUTME: Sets up logging, signal handlers, and runs the Worker main loop
"""Worker entry point: python -m archiver."""

from __future__ import annotations

import asyncio
import signal

from beartype import beartype

from archiver.config import Settings
from archiver.logging import setup_logging
from archiver.worker import Worker


@beartype
def main() -> None:
    """Start the worker process."""
    settings = Settings()
    setup_logging(settings.log_level, settings.log_format)

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
