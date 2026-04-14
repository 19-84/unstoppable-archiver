# ABOUTME: Unit tests for the worker entry point (__main__.py)
# ABOUTME: Verifies Settings creation, logging setup, Worker run, and signal handlers
"""Tests for worker entry point."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestMain:
    @patch("archiver.__main__.asyncio")
    @patch("archiver.__main__.Worker")
    @patch("archiver.__main__.setup_logging")
    @patch("archiver.__main__.Settings")
    def test_main_creates_worker_and_runs(
        self,
        mock_settings_cls: MagicMock,
        mock_setup_logging: MagicMock,
        mock_worker_cls: MagicMock,
        mock_asyncio: MagicMock,
    ) -> None:
        from archiver.__main__ import main

        mock_settings = MagicMock()
        mock_settings.log_level = "INFO"
        mock_settings.log_format = "json"
        mock_settings_cls.return_value = mock_settings

        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        mock_worker.shutdown = AsyncMock()
        mock_worker_cls.return_value = mock_worker

        mock_loop = MagicMock()
        mock_asyncio.new_event_loop.return_value = mock_loop

        main()

        mock_settings_cls.assert_called_once()
        mock_setup_logging.assert_called_once_with("INFO", "json")
        mock_worker_cls.assert_called_once_with(mock_settings)
        mock_loop.run_until_complete.assert_called_once()
        mock_loop.close.assert_called_once()

    @patch("archiver.__main__.asyncio")
    @patch("archiver.__main__.Worker")
    @patch("archiver.__main__.setup_logging")
    @patch("archiver.__main__.Settings")
    def test_main_registers_signal_handlers(
        self,
        mock_settings_cls: MagicMock,
        mock_setup_logging: MagicMock,
        mock_worker_cls: MagicMock,
        mock_asyncio: MagicMock,
    ) -> None:
        from archiver.__main__ import main

        mock_settings = MagicMock()
        mock_settings.log_level = "INFO"
        mock_settings.log_format = "json"
        mock_settings_cls.return_value = mock_settings

        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        mock_worker.shutdown = AsyncMock()
        mock_worker_cls.return_value = mock_worker

        mock_loop = MagicMock()
        mock_asyncio.new_event_loop.return_value = mock_loop

        main()

        # Two signal handlers: SIGINT and SIGTERM
        assert mock_loop.add_signal_handler.call_count == 2  # noqa: PLR2004
