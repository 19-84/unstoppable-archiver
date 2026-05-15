# ABOUTME: Job processing worker with LISTEN/NOTIFY and tier escalation
# ABOUTME: Polls PostgreSQL for capture jobs, runs pipeline, handles retries and fallbacks
"""Standalone worker process for capture job processing."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import structlog
from beartype import beartype

from archiver.browser_pool import BrowserPool
from archiver.capture import capture_page, save_artifacts
from archiver.commoncrawl import (
    fetch_record_html as cc_fetch_record_html,
)
from archiver.commoncrawl import (
    find_snapshot as cc_find_snapshot,
)
from archiver.commoncrawl import (
    find_snapshot_full_history as cc_find_snapshot_full_history,
)
from archiver.config import Settings
from archiver.cookie_cache import CfClearanceCache
from archiver.db import create_pool, init_db
from archiver.enums import (
    CLEARNET_TIER_ORDER,
    ArchiveStatus,
    CaptureSource,
    CaptureTier,
)
from archiver.errors import AntiBotDetectedError, CaptureError
from archiver.fallback import (
    ARCHIVE_TODAY_MIRRORS as _ARCHIVE_TODAY_MIRRORS,
)
from archiver.fallback import (
    ARCHIVE_TODAY_STRIP_SELECTORS,
    WAYBACK_STRIP_SELECTORS,
    check_wayback_availability,
    extract_title_from_html,
    fetch_archive_today_snapshot_html,
    find_archive_today_snapshot,
    save_to_archive_today,
    save_to_wayback,
    strip_html_tags,
)
from archiver.metrics import (
    artifacts_dir_bytes_free,
    artifacts_dir_bytes_total,
    artifacts_dir_bytes_used,
    capture_duration_seconds,
    captures_total,
    jobs_running,
)
from archiver.models import CaptureResult, JobRecord
from archiver.proxy import (
    ProxyConfig,
    ProxyRotator,
    filter_by_asn,
    filter_gate_passing,
    filter_healthy,
    filter_socks5,
    load_proxies,
)
from archiver.repository import (
    ArchiveRepository,
    DomainObservationsRepository,
    FrontendStatusRepository,
    JobRepository,
    PgConnection,
    ProxyStatusRepository,
)
from archiver.url import apex_of

log = structlog.get_logger()

# Minimal valid 1x1 transparent PNG for direct-fetch fallback snapshots
# that skip the browser entirely and so have no real screenshot.
_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _log_task_exception(task: asyncio.Task[Any]) -> None:  # pragma: no cover
    """Log any unhandled exception from a background task."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "worker.task_exception",
            error=str(exc),
            exc_info=exc,
        )


@beartype
def next_tier(current: CaptureTier) -> CaptureTier | None:
    """Return the next escalation tier, or None if exhausted."""
    try:
        idx = CLEARNET_TIER_ORDER.index(current)
    except ValueError:
        return None
    next_idx = idx + 1
    if next_idx >= len(CLEARNET_TIER_ORDER):
        return None
    return CLEARNET_TIER_ORDER[next_idx]


class Worker:
    """Capture job processor.

    Listens for PostgreSQL NOTIFY events and processes capture jobs
    with automatic tier escalation on anti-bot detection.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser_pool = BrowserPool(settings)
        self._archive_repo = ArchiveRepository()
        self._job_repo = JobRepository()
        self._obs_repo = DomainObservationsRepository()
        self._proxy_status_repo = ProxyStatusRepository()
        self._frontend_status_repo = FrontendStatusRepository()
        self._cookie_cache = CfClearanceCache()
        self._semaphore = asyncio.Semaphore(
            settings.max_concurrent_captures
        )
        self._running = True
        self._pool: asyncpg.Pool | None = None
        # Background tasks tracked here include ones with non-None return
        # types (user_agents.refresh returns int, etc.) — keep the set
        # type heterogeneous so we don't have to thread `Task[Any]`
        # through every call site.
        self._tasks: set[asyncio.Task[Any]] = set()
        self._capture_tasks: set[asyncio.Task[Any]] = set()
        self._proxy_rotator: ProxyRotator = ProxyRotator()

    @beartype
    async def run(self) -> None:  # pragma: no cover  # noqa: PLR0915
        """Main worker loop."""
        self._pool = await create_pool(
            self._settings.db_url.get_secret_value(), min_size=2, max_size=5
        )
        await init_db(self._pool)

        # Refresh the User-Agent rotation pool from the daily-updated
        # public source. Bundled fallback covers us if the fetch fails.
        from archiver import user_agents as _ua
        ua_task = asyncio.create_task(_ua.refresh(force=True))
        self._tasks.add(ua_task)
        ua_task.add_done_callback(self._tasks.discard)
        ua_task.add_done_callback(_log_task_exception)

        # Kick off proxy loading + health-checking as a background task.
        # Large public lists (7k+ entries) can take 15+ min to probe; we
        # don't want to block accepting work on that. CAMOUFOX_PROXY jobs
        # running before the check completes see an empty rotator and
        # fall through to direct Camoufox, which is still better than
        # crashing or waiting.
        proxy_task = asyncio.create_task(self._init_proxy_rotator())
        self._tasks.add(proxy_task)
        proxy_task.add_done_callback(self._tasks.discard)
        proxy_task.add_done_callback(_log_task_exception)

        # Periodic UA-pool refresh every 6 hours — upstream updates its
        # dataset daily. Runs for the worker's lifetime.
        ua_periodic = asyncio.create_task(self._ua_refresh_loop())
        self._tasks.add(ua_periodic)
        ua_periodic.add_done_callback(self._tasks.discard)
        ua_periodic.add_done_callback(_log_task_exception)

        # Background gate-probing against archive.ph. Keeps proxy_status
        # warm so tier-5 reads don't go direct (our server IP is walled).
        gate_task = asyncio.create_task(self._gate_probe_loop())
        self._tasks.add(gate_task)
        gate_task.add_done_callback(self._tasks.discard)
        gate_task.add_done_callback(_log_task_exception)

        # Background frontend-probing. Verifies Scribe/xcancel/Redlib
        # instances serve real content, not Anubis challenge shells —
        # capture-time routing only picks content-verified instances.
        frontend_task = asyncio.create_task(self._frontend_probe_loop())
        self._tasks.add(frontend_task)
        frontend_task.add_done_callback(self._tasks.discard)
        frontend_task.add_done_callback(_log_task_exception)

        # Background disk-usage sampler. Archives never expire, so the
        # artifact volume grows monotonically — the gauge feeds the
        # Prometheus rule that pages the operator at >80 % used.
        disk_task = asyncio.create_task(self._disk_usage_loop())
        self._tasks.add(disk_task)
        disk_task.add_done_callback(self._tasks.discard)
        disk_task.add_done_callback(_log_task_exception)

        # Reclaim stale jobs from dead workers
        async with self._pool.acquire() as conn:
            reclaimed = await self._job_repo.reclaim_stale(conn)
            if reclaimed > 0:
                log.info(
                    "worker.reclaimed_stale",
                    count=reclaimed,
                )

        # Set up LISTEN for new_job notifications
        listen_conn = await self._pool.acquire()
        try:
            await listen_conn.add_listener(
                "new_job", self._on_notify
            )

            log.info(
                "worker.started",
                worker_id=self._settings.worker_id,
            )

            while self._running:
                await self._claim_and_process()
                # Sleep in short increments so shutdown is responsive
                for _ in range(int(self._settings.worker_poll_interval * 10)):
                    if not self._running:
                        break
                    await asyncio.sleep(0.1)
        finally:
            # Drain in-flight jobs before tearing down (graceful shutdown).
            # Bounded by 2x max_capture_timeout to prevent hung jobs from
            # blocking shutdown forever.
            if self._tasks:
                drain_timeout = self._settings.max_capture_timeout * 2
                log.info(
                    "worker.draining",
                    in_flight=len(self._tasks),
                    timeout_s=drain_timeout,
                )
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *self._tasks, return_exceptions=True
                        ),
                        timeout=drain_timeout,
                    )
                    log.info("worker.drained")
                except TimeoutError:
                    log.warning(
                        "worker.drain_timeout",
                        stuck=len(self._tasks),
                    )

            await listen_conn.remove_listener(
                "new_job", self._on_notify
            )
            await self._pool.release(listen_conn)
            await self._browser_pool.close()
            await self._pool.close()
            log.info("worker.stopped")

    # No @beartype — asyncpg callback, types guaranteed by the library
    def _on_notify(
        self,
        connection: PgConnection,
        pid: int,
        channel: str,
        payload: object,
    ) -> None:
        """Handle LISTEN/NOTIFY callback — wake the worker."""
        log.debug(
            "worker.notify_received",
            channel=channel,
            payload=payload,
        )

    @beartype
    async def _claim_and_process(self) -> None:  # pragma: no cover
        """Try to claim and process one job."""
        assert self._pool is not None  # noqa: S101

        # Don't claim if at capacity — prevents unbounded task accumulation.
        # Only count capture tasks, not background housekeeping tasks.
        if len(self._capture_tasks) >= self._settings.max_concurrent_captures:
            return

        async with self._pool.acquire() as conn:
            job = await self._job_repo.claim_next(
                conn, self._settings.worker_id
            )
        if job is None:
            return

        task = asyncio.create_task(self._process_job(job))
        self._tasks.add(task)
        self._capture_tasks.add(task)
        task.add_done_callback(self._capture_tasks.discard)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_task_exception)

    @beartype
    async def _process_job(self, job: JobRecord) -> None:
        """Process a single capture job with tier escalation."""
        async with self._semaphore:
            assert self._pool is not None  # noqa: S101
            jobs_running.inc()
            outcome = "failed"
            try:
                with capture_duration_seconds.labels(
                    tier=job.tier.value
                ).time():
                    outcome = await self._process_job_inner(job)
            finally:
                jobs_running.dec()
                captures_total.labels(
                    tier=job.tier.value, outcome=outcome
                ).inc()

    @beartype
    async def _process_job_inner(self, job: JobRecord) -> str:  # noqa: C901, PLR0911, PLR0915
        """Inner job processing; returns outcome label for metrics."""
        assert self._pool is not None  # noqa: S101
        apex = ""  # bound for the except handlers; set properly once archive is fetched
        async with self._pool.acquire() as conn:
                try:
                    archive = await self._archive_repo.get_by_id(
                        conn, job.archive_id
                    )
                    if archive is None:
                        await self._job_repo.fail(
                            conn,
                            job.id,
                            "Archive deleted before capture",
                        )
                        return "failed"

                    # Short-circuit: if another job already captured this
                    # archive (tier escalation can race when a retry runs
                    # after a previous attempt finished), skip the
                    # redundant work. Without this early-exit, extra
                    # queued jobs re-run captures and revert a COMPLETE
                    # archive back to CAPTURING.
                    if archive.status == ArchiveStatus.COMPLETE:
                        await self._job_repo.complete(conn, job.id)
                        log.info(
                            "worker.skipped_already_complete",
                            job_id=job.id,
                            archive_id=archive.id,
                        )
                        return "complete"

                    # Computed up-front so the exception handlers below
                    # can record the per-tier loss even if update_status
                    # or a capture call raises before the happy path.
                    apex = apex_of(archive.url)

                    await self._archive_repo.update_status(
                        conn,
                        job.archive_id,
                        ArchiveStatus.CAPTURING,
                    )

                    # Fallback tiers use public archives instead of
                    # direct capture. Every tier's actual capture call
                    # is wrapped in wait_for(max_capture_timeout) so a
                    # hung Camoufox / dead SOCKS5 / runaway SingleFile
                    # surfaces as TimeoutError (caught below, marked as
                    # this tier's failure, escalated to the next tier)
                    # instead of wedging the worker.
                    async def _dispatch() -> CaptureResult:  # noqa: PLR0911
                        if job.tier == CaptureTier.WAYBACK:
                            return await self._capture_via_wayback(
                                archive.url,
                            )
                        if job.tier == CaptureTier.ARCHIVE_TODAY:
                            return await self._capture_via_archive_today(
                                archive.url,
                            )
                        if job.tier == CaptureTier.COMMONCRAWL:
                            return await self._capture_via_commoncrawl(
                                archive.url,
                            )
                        if job.tier == CaptureTier.ARCHIVE_TODAY_SUBMIT:
                            return await self._capture_via_archive_today_submit(
                                archive.url,
                            )
                        if job.tier == CaptureTier.PRIVACY_FRONTEND:
                            return await self._capture_via_privacy_frontend(
                                archive.url,
                            )
                        browser = await self._browser_pool.get_browser(
                            job.tier,
                        )
                        # CAMOUFOX_PROXY tier pulls a rotating proxy
                        # from the pool; other tiers go direct (None).
                        tier_proxy: ProxyConfig | None = None
                        if job.tier == CaptureTier.CAMOUFOX_PROXY:
                            tier_proxy = self._proxy_rotator.next()
                            if tier_proxy is None:
                                log.warning(
                                    "worker.camoufox_proxy_no_proxy"
                                    "_available_going_direct",
                                    job_id=job.id,
                                )
                        return await self._capture_page_with_proxy_retry(
                            job, archive.url, browser, tier_proxy,
                        )

                    result = await asyncio.wait_for(
                        _dispatch(),
                        timeout=self._settings.max_capture_timeout,
                    )

                    artifact_dir = await save_artifacts(
                        result,
                        job.archive_id,
                        self._settings.artifacts_dir,
                    )

                    source = CaptureSource.DIRECT
                    if job.tier == CaptureTier.WAYBACK:
                        source = CaptureSource.WAYBACK
                    elif job.tier in (
                        CaptureTier.ARCHIVE_TODAY,
                        CaptureTier.ARCHIVE_TODAY_SUBMIT,
                    ):
                        source = CaptureSource.ARCHIVE_TODAY
                    elif job.tier == CaptureTier.COMMONCRAWL:
                        source = CaptureSource.COMMONCRAWL
                    elif job.tier == CaptureTier.PRIVACY_FRONTEND:
                        source = CaptureSource.PRIVACY_FRONTEND

                    # source_url provenance — recorded in metadata
                    # so the UI can show "captured from <instance>"
                    # without losing the original submission URL.
                    metadata_json: str | None = None
                    if result.source_url is not None:
                        import json as _json
                        metadata_json = _json.dumps(
                            {"source_url": result.source_url},
                        )

                    await self._archive_repo.update_status(
                        conn,
                        job.archive_id,
                        ArchiveStatus.COMPLETE,
                        title=result.title,
                        text_content=result.text_content,
                        artifact_dir=artifact_dir,
                        content_hash=result.content_hash,
                        screenshot_hash=result.screenshot_hash,
                        snapshot_size=len(result.snapshot_html),
                        warc_size=result.warc_size,
                        source=source.value,
                        metadata=metadata_json,
                    )
                    await self._job_repo.complete(conn, job.id)
                    await self._obs_repo.record_outcome(
                        conn, apex, job.tier, won=True,
                    )
                    return "complete"

                except AntiBotDetectedError as exc:
                    await self._handle_antibot(
                        conn, job, str(exc), apex=apex,
                    )
                    return "antibot"

                except CaptureError as exc:
                    await self._handle_capture_error(
                        conn, job, str(exc), apex=apex,
                    )
                    return "failed"

                except Exception as exc:
                    log.exception(
                        "worker.unexpected_error",
                        job_id=job.id,
                    )
                    await self._handle_capture_error(
                        conn, job, str(exc), apex=apex,
                    )
                    return "failed"

    # @beartype — private, conn is AsyncMock in tests
    async def _handle_antibot(
        self,
        conn: PgConnection,
        job: JobRecord,
        error: str,
        apex: str = "",
    ) -> None:
        """Escalate to next tier on anti-bot detection."""
        escalated = next_tier(job.tier)
        await self._job_repo.fail(
            conn, job.id, error, retry=False
        )
        await self._obs_repo.record_outcome(
            conn, apex, job.tier, won=False,
        )

        if escalated is not None:
            log.warning(
                "worker.tier_escalation",
                job_id=job.id,
                from_tier=job.tier.value,
                to_tier=escalated.value,
            )
            await self._job_repo.enqueue(
                conn,
                job.archive_id,
                escalated,
                priority=job.priority + 1,
            )
        else:
            log.error(
                "worker.all_tiers_exhausted",
                job_id=job.id,
            )
            await self._archive_repo.update_status(
                conn,
                job.archive_id,
                ArchiveStatus.FAILED,
                error_message="All capture tiers exhausted: "
                + error,
            )

    # @beartype — private, conn is AsyncMock in tests
    async def _handle_capture_error(
        self,
        conn: PgConnection,
        job: JobRecord,
        error: str,
        apex: str = "",
    ) -> None:
        """Handle capture failure with optional retry."""
        # Count total attempts at this tier (each retry creates a new job,
        # so we count jobs rather than relying on the per-job counter).
        tier_attempts = int(
            await conn.fetchval(
                "SELECT count(*) FROM jobs WHERE archive_id = $1 AND tier = $2",
                job.archive_id,
                job.tier.value,
            )
            or 0
        )
        if tier_attempts < job.max_attempts:
            log.warning(
                "worker.retry",
                job_id=job.id,
                attempt=tier_attempts,
                max=job.max_attempts,
            )
            await self._job_repo.fail(
                conn, job.id, error, retry=True
            )
        else:
            # Exhausted retries on this tier — escalate to next tier
            escalated = next_tier(job.tier)
            await self._job_repo.fail(
                conn, job.id, error, retry=False
            )
            await self._obs_repo.record_outcome(
                conn, apex, job.tier, won=False,
            )
            if escalated is not None:
                log.warning(
                    "worker.tier_escalation_after_retries",
                    job_id=job.id,
                    from_tier=job.tier.value,
                    to_tier=escalated.value,
                    attempts=tier_attempts,
                )
                await self._job_repo.enqueue(
                    conn,
                    job.archive_id,
                    escalated,
                    priority=job.priority + 1,
                )
            else:
                log.error(
                    "worker.all_tiers_exhausted",
                    job_id=job.id,
                    tier=job.tier.value,
                    attempts=tier_attempts,
                )
                await self._archive_repo.update_status(
                    conn,
                    job.archive_id,
                    ArchiveStatus.FAILED,
                    error_message=error,
                )

    async def _capture_page_with_proxy_retry(
        self,
        job: JobRecord,
        url: str,
        browser: Any,
        proxy: ProxyConfig | None,
    ) -> CaptureResult:
        """Run capture_page; mark proxy failed and raise on error.

        Marking a proxy failed removes it from the rotator for this
        worker's lifetime, so subsequent CAMOUFOX_PROXY jobs skip it.
        The retry itself happens at the job/tier layer, not here — we
        don't want one capture to serially try N proxies.
        """
        try:
            return await capture_page(
                url=url,
                browser=browser,
                settings=self._settings,
                tier=job.tier,
                cookie_cache=self._cookie_cache,
                proxy=proxy,
            )
        except Exception:
            if proxy is not None:
                self._proxy_rotator.mark_failed(proxy)
            raise

    async def _capture_via_wayback(self, url: str) -> CaptureResult:
        """Capture a page via Wayback Machine fallback.

        Flow:
        1. Check if URL is already archived (with URL variant fallback).
        2. If not, submit via Save Page Now — this both creates a
           permanent public record AND gives us a snapshot URL to
           capture from.
        3. Capture the snapshot URL, stripping Wayback toolbar chrome
           so our archive contains only the original page.
        """
        snapshot_url = await check_wayback_availability(url)
        if not snapshot_url:
            log.info("worker.wayback.spn_attempting", url=url)
            browser = await self._browser_pool.get_browser(
                CaptureTier.CHROMIUM
            )
            context = await browser.new_context()
            try:
                page = await context.new_page()
                snapshot_url = await save_to_wayback(url, page)
            finally:
                await context.close()
            if not snapshot_url:
                raise CaptureError(
                    f"URL not in Wayback and SPN submission failed: {url}"
                )

        browser = await self._browser_pool.get_browser(CaptureTier.CHROMIUM)
        result = await capture_page(
            url=snapshot_url,
            browser=browser,
            settings=self._settings,
            tier=CaptureTier.CHROMIUM,
            strip_selectors=WAYBACK_STRIP_SELECTORS,
            warc_original_url=url,
        )
        from dataclasses import replace
        return replace(result, source_url=snapshot_url)

    async def _capture_via_commoncrawl(self, url: str) -> CaptureResult:
        """Last-tier fallback: fetch a cached version from Common Crawl.

        Two-pass lookup:
        1. Fast path — 3 most recent crawls in parallel (~1-3s).
        2. Deep scan — on miss, sequentially walks every crawl back
           to 2014. Paced at ~5 req/s so a full 122-crawl sweep takes
           ~30-60s. Catches the long-tail case where a URL was only
           ever crawled years ago (indie blogs, defunct sites).

        Raises CaptureError if both passes miss. The snapshot.html is
        the raw HTTP response body CC captured at crawl time; it's NOT
        self-contained (external images/CSS reference originals).
        """
        snapshot = await cc_find_snapshot(url)
        if snapshot is None:
            log.info("worker.commoncrawl.recent_miss_deep_scanning", url=url)
            snapshot = await cc_find_snapshot_full_history(url)
        if snapshot is None:
            raise CaptureError(
                f"No Common Crawl snapshot across all crawls: {url}"
            )
        try:
            body = await cc_fetch_record_html(snapshot)
        except Exception as exc:
            raise CaptureError(
                f"Common Crawl range-fetch failed: {exc}"
            ) from exc
        html_str = body.decode("utf-8", errors="replace")
        # Build a result with CC's original URL as the source marker
        # so the archive records the true crawl-time URL (may differ
        # from the user-submitted URL due to server-side canonicalization).
        log.info(
            "worker.commoncrawl.snapshot_used",
            url=url,
            cc_url=snapshot.url,
            crawl=snapshot.crawl_id,
            timestamp=snapshot.timestamp,
        )
        return self._capture_result_from_html(html_str, snapshot.url)

    async def _capture_via_archive_today(self, url: str) -> CaptureResult:
        """Capture a page via archive.today fallback.

        Tier 5 is read-only — it looks for existing snapshots across
        all mirrors, fetches the raw HTML directly (bypassing CF), and
        only falls through to a full Camoufox render if direct-fetch
        hits a challenge.

        We deliberately do NOT submit new captures here — Wayback SPN
        in tier 4 already handles that role, submission-via-Camoufox
        was slow (30-120 s) and CF-gated, and archive.today publishes
        timemap specifically as a machine-friendly read interface.
        """
        # Archive.today's CF edge scores our server IP poorly enough
        # that direct-IP timemap/snapshot reads nearly always 4xx.
        # Route through a gate-passing SOCKS5 from proxy_status when
        # one is available; fall back to direct if the pool is empty.
        at_proxy = await self._pick_archive_today_proxy()

        snapshot_url = await find_archive_today_snapshot(url, proxy=at_proxy)
        if snapshot_url is None:
            raise CaptureError(
                f"No archive.today snapshot across {len(_ARCHIVE_TODAY_MIRRORS)}"
                f" mirrors: {url}"
            )

        # Try direct-fetch first — fast (1-3 s), bypasses CF challenge
        # by targeting the static snapshot URL with stealth headers.
        raw_html = await fetch_archive_today_snapshot_html(
            snapshot_url, proxy=at_proxy
        )
        if raw_html is not None:
            return self._capture_result_from_html(
                raw_html, snapshot_url
            )

        # Fallback: full Camoufox render against the memento URL.
        log.info(
            "worker.archive_today.direct_fetch_failed_fallback_browser",
            memento=snapshot_url,
        )
        from dataclasses import replace
        browser = await self._browser_pool.get_browser(CaptureTier.CAMOUFOX)
        result = await capture_page(
            url=snapshot_url,
            browser=browser,
            settings=self._settings,
            tier=CaptureTier.CAMOUFOX,
            strip_selectors=ARCHIVE_TODAY_STRIP_SELECTORS,
            warc_original_url=url,
        )
        # Record the memento URL as the provenance source — without
        # this, the browser-fallback path silently dropped source_url
        # while the sibling direct-fetch path (above) preserved it.
        # The detail page's 'Captured from' block would then disappear
        # for the exact subset of archive.today captures that needed a
        # full render to bypass CF.
        return replace(result, source_url=snapshot_url)

    async def _capture_via_archive_today_submit(
        self, url: str
    ) -> CaptureResult:
        """Submit URL to archive.today and fetch the resulting memento.

        Last-resort write path — all read tiers have already failed to
        find an existing copy anywhere. We're imposing real load on a
        volunteer free service so this runs at most once per URL.

        Requires a gate-passing SOCKS5: the submission form is CF-gated
        + Turnstile, and our direct IP gets walled. If the pool is empty
        we fail out immediately rather than 403-ing our way through.
        """
        at_proxy = await self._pick_archive_today_proxy()
        if at_proxy is None:
            raise CaptureError(
                "archive.today submit: no gate-passing proxy available"
            )

        # One-shot Camoufox bound to the gate-passer. Can't reuse the
        # browser_pool's shared Camoufox — proxy is a launch-time arg.
        from camoufox.async_api import (  # type: ignore[import-untyped]
            AsyncCamoufox,
        )

        snapshot_url: str | None = None
        async with AsyncCamoufox(
            headless=self._settings.camoufox_headless,
            humanize=True,
            geoip=False,  # see probe_archive_gate for rationale
            proxy={"server": at_proxy},
        ) as browser_:
            # Camoufox ships no type stubs and its async context returns
            # an object pyright misreads as BrowserContext; the runtime
            # type behaves like Playwright Browser. Cast to Any locally
            # so member access doesn't cascade Unknown into our own code.
            browser: Any = browser_
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            try:
                snapshot_url = await save_to_archive_today(url, page)
            finally:
                await page.close()
                await context.close()

        if snapshot_url is None:
            raise CaptureError(
                f"archive.today submit failed for {url}"
            )

        log.info(
            "worker.archive_today.submit_success",
            url=url,
            snapshot=snapshot_url,
        )

        # Fetch the fresh memento through the same proxy — CF edge walls
        # direct-IP reads on newly-created snapshots just like on old ones.
        raw_html = await fetch_archive_today_snapshot_html(
            snapshot_url, proxy=at_proxy
        )
        if raw_html is None:
            raise CaptureError(
                "archive.today submit succeeded but memento fetch failed: "
                f"{snapshot_url}"
            )
        return self._capture_result_from_html(raw_html, snapshot_url)

    async def _capture_via_privacy_frontend(
        self, url: str
    ) -> CaptureResult:
        """Route `url` through a registered privacy frontend.

        Raises CaptureError when the URL has no registered frontend
        (→ immediate escalation to tier 5) or when every instance in
        the policy's list fails. Each instance attempt goes through
        Camoufox + a gate-passing SOCKS5 so Anubis/CF challenges on
        the frontend itself can resolve.
        """
        from archiver.privacy_frontends import (
            resolve_policy,
            rewrite_to_instance,
        )

        policy = resolve_policy(url)
        if policy is None:
            raise CaptureError(
                f"No privacy frontend registered for {url}"
            )

        # SOCKS5 is preferred but optional: many frontend instances are
        # Anubis-walled (JS PoW that Camoufox clears regardless of source
        # IP), and only the CF-protected subset actually needs a
        # residential-looking exit. When the gate-passing pool is empty
        # we fall through to direct Camoufox — Anubis-walled instances
        # still work, CF-walled ones fail and the per-instance loop
        # moves on to the next candidate.
        at_proxy = await self._pick_archive_today_proxy()
        proxy_config: ProxyConfig | None = None
        if at_proxy is not None:
            proxy_config = ProxyConfig(server=at_proxy)
        else:
            log.warning(
                "worker.privacy_frontend.no_proxy_falling_back_direct",
                target=policy.target_apex,
            )

        # Only try instances that the background probe loop has
        # recently confirmed serve real content (not an Anubis or CF
        # shell). Before the probe has run on a fresh environment this
        # list is empty and the tier escalates cleanly to wayback —
        # storing a challenge page as "content" is worse than escalating.
        assert self._pool is not None  # noqa: S101
        async with self._pool.acquire() as conn:
            verified = await self._frontend_status_repo.list_passing(
                conn, policy.target_apex,
            )
        if not verified:
            raise CaptureError(
                f"privacy frontend: no content-verified instance for "
                f"{policy.target_apex}"
            )

        browser = await self._browser_pool.get_browser(CaptureTier.CAMOUFOX)
        last_error: str | None = None
        for instance in verified:
            rewritten = rewrite_to_instance(url, instance)
            log.info(
                "worker.privacy_frontend.attempting",
                original_url=url,
                instance=instance,
                rewritten=rewritten,
            )
            try:
                result = await capture_page(
                    url=rewritten,
                    browser=browser,
                    settings=self._settings,
                    tier=CaptureTier.CAMOUFOX,
                    proxy=proxy_config,
                    warc_original_url=url,
                )
            except (AntiBotDetectedError, CaptureError) as exc:
                last_error = str(exc)
                log.warning(
                    "worker.privacy_frontend.instance_failed",
                    instance=instance,
                    error=last_error[:120],
                )
                continue

            # Per-URL not-found check. Probe verified the instance is
            # up; this catches the case where the instance can't serve
            # the specific URL we asked for and returns a 200 OK
            # error shell. Without this, scribe's "This article is
            # missing" or libmedium's "502 Bad Gateway" gets stored as
            # content. Each policy enumerates every shell pattern its
            # instances are known to emit.
            hit_marker = next(
                (
                    m for m in policy.not_found_markers
                    if m.encode() in result.snapshot_html
                ),
                None,
            )
            if hit_marker is not None:
                last_error = (
                    f"instance returned not-found marker "
                    f"({hit_marker!r})"
                )
                log.warning(
                    "worker.privacy_frontend.instance_not_found",
                    instance=instance,
                    marker=hit_marker,
                )
                continue

            log.info(
                "worker.privacy_frontend.success",
                original_url=url,
                instance=instance,
            )
            # Record the rewritten URL we actually captured so the UI
            # can show which instance won.
            from dataclasses import replace
            return replace(result, source_url=rewritten)

        raise CaptureError(
            f"All privacy frontend instances failed for {url}: "
            f"{last_error}"
        )

    def _capture_result_from_html(
        self, html: str, source_url: str
    ) -> CaptureResult:
        """Build a CaptureResult from raw HTML (direct-fetch path).

        No screenshot, no WARC — direct-fetch doesn't go through a
        browser. We substitute a placeholder PNG so the artifact layout
        stays consistent and the UI doesn't 404 on missing images.
        """
        import hashlib

        snapshot_html = html.encode("utf-8")
        placeholder_png = _PLACEHOLDER_PNG
        return CaptureResult(
            snapshot_html=snapshot_html,
            screenshot_png=placeholder_png,
            thumbnail_png=placeholder_png,
            text_content=strip_html_tags(html),
            title=extract_title_from_html(html) or source_url,
            warc_path=None,
            warc_size=0,
            content_hash=hashlib.sha256(snapshot_html).hexdigest(),
            screenshot_hash=hashlib.sha256(
                placeholder_png
            ).hexdigest(),
            # Direct-fetch tiers (CC, archive.today read, wayback
            # browser-render-of-memento) feed source_url through so
            # the archive metadata records exactly where the bytes came from.
            source_url=source_url,
        )

    @beartype
    async def shutdown(self) -> None:
        """Gracefully stop the worker."""
        log.info("worker.shutting_down")
        self._running = False

    async def _ua_refresh_loop(self) -> None:  # pragma: no cover
        """Refresh the UA pool every 6h.

        Upstream dataset updates daily; 6h cadence keeps us fresh
        without being noisy. Idempotent — internal staleness check
        suppresses the HTTP fetch when the cache is recent.
        """
        from archiver import user_agents as _ua
        while self._running:
            for _ in range(6 * 60 * 60 // 100):  # sleep in 100ms ticks
                if not self._running:
                    return
                await asyncio.sleep(0.1)
            try:
                await _ua.refresh()
            except Exception as exc:
                log.warning("worker.ua_refresh_failed", error=str(exc))

    async def _disk_usage_loop(self) -> None:  # pragma: no cover
        """Sample artifact-volume usage every 60 s and update gauges.

        Archives are kept forever by design, so disk grows monotonically
        and the operator needs warning before the volume fills. The
        gauges (artifacts_dir_bytes_total / _used / _free) feed the
        Prometheus rule at docs/observability.md; sample cadence is
        deliberately slow because `shutil.disk_usage` is a statvfs call,
        not expensive, but emitting more often than every minute just
        churns the scrape with no actual signal.
        """
        import shutil

        path = self._settings.artifacts_dir
        # Make sure the dir exists on first iteration — statvfs against
        # a missing path raises FileNotFoundError, and the worker can
        # legitimately start before any artifact is written.
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning(
                "worker.disk_usage.mkdir_failed",
                path=str(path), error=str(exc),
            )
        while self._running:
            try:
                usage = shutil.disk_usage(path)
                artifacts_dir_bytes_total.set(usage.total)
                artifacts_dir_bytes_used.set(usage.used)
                artifacts_dir_bytes_free.set(usage.free)
            except OSError as exc:
                log.warning(
                    "worker.disk_usage.sample_failed",
                    path=str(path), error=str(exc),
                )
            for _ in range(60):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _init_proxy_rotator(self) -> None:  # pragma: no cover
        """Load proxies from config + URL lists, optionally health-check.

        Runs once at startup. Failures are logged but non-fatal — the
        worker can still function without any proxies (CAMOUFOX_PROXY
        tier simply degrades to direct Camoufox when the rotator is empty).
        """
        try:
            candidates = await load_proxies(
                self._settings.proxy_list,
                self._settings.proxy_list_urls,
                default_scheme=self._settings.proxy_default_scheme,
                max_count=self._settings.proxy_max_count,
            )
        except Exception as exc:
            log.warning("worker.proxy_load_failed", error=str(exc))
            return

        if not candidates:
            log.info("worker.proxy_list_empty")
            return

        if self._settings.proxy_health_check_enabled:
            log.info(
                "worker.proxy_health_check_start",
                candidates=len(candidates),
            )
            healthy = await filter_healthy(
                candidates,
                probe_url=self._settings.proxy_health_check_url,
                timeout=self._settings.proxy_health_check_timeout,
                concurrency=(
                    self._settings.proxy_health_check_concurrency
                ),
            )
        else:
            healthy = candidates

        self._proxy_rotator = ProxyRotator(proxies=healthy)
        log.info(
            "worker.proxy_rotator_ready",
            total=len(candidates),
            healthy=len(healthy),
        )

    async def _pick_archive_today_proxy(self) -> str | None:
        """Return a fresh gate-passing SOCKS5, or None if pool is empty."""
        assert self._pool is not None  # noqa: S101
        async with self._pool.acquire() as conn:
            passing = await self._proxy_status_repo.list_passing(
                conn, max_age_hours=24
            )
        if not passing:
            log.warning("worker.archive_today.no_gate_passers")
            return None
        import random
        return random.choice(passing)  # noqa: S311 — not security-sensitive

    async def _gate_probe_loop(self) -> None:  # pragma: no cover
        """Periodically gate-probe SOCKS5 candidates against archive.ph.

        Each iteration does two things:

        1. **Re-verify** the oldest still-passing entries. The 24 h
           freshness window means a row marked `gate_passing=true` 23 h
           ago still counts as passing today — even if the proxy died
           hours ago. Re-probing oldest-first flips those to
           `passing=false` before the capture path tries to use them.

        2. **Probe new candidates** from the rotator. ASN-filtered for
           consumer first, unfiltered fallback when the consumer pool
           is empty (cloud-hosted SOCKS5 dominate public lists).

        Cadence is adaptive on the current pool depth:
        - <5 fresh passers      → 10 min sleep, batch size 20 (urgent)
        - <15 fresh passers     → 30 min sleep, batch size 15 (building)
        - 15+ fresh passers     → 60 min sleep, batch size 10 (healthy)

        Each Camoufox probe costs ~6-15 s; concurrency 3 keeps load on
        archive.ph well below their tolerance even at the urgent rate.
        """
        # Event-driven warmup: wait until the rotator has been
        # populated by filter_healthy. A static timer races
        # filter_healthy on large lists.
        while self._running and not self._proxy_rotator.proxies:
            await asyncio.sleep(5)
        while self._running:
            try:
                await self._gate_probe_iter()
            except Exception as exc:
                log.warning(
                    "worker.gate_probe_iter_failed", error=str(exc)
                )
            # Pool-depth-adaptive sleep.
            assert self._pool is not None  # noqa: S101
            async with self._pool.acquire() as conn:
                pool_size = len(
                    await self._proxy_status_repo.list_passing(conn)
                )
            if pool_size < 5:                       # noqa: PLR2004
                sleep_s = 600   # 10 min
            elif pool_size < 15:                    # noqa: PLR2004
                sleep_s = 1800  # 30 min
            else:
                sleep_s = 3600  # 1 h
            log.info(
                "worker.gate_probe_sleeping",
                pool_size=pool_size, sleep_s=sleep_s,
            )
            for _ in range(sleep_s):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _gate_probe_iter(self) -> None:  # pragma: no cover
        """One pass: re-verify oldest passers, then probe new candidates."""
        assert self._pool is not None  # noqa: S101

        # Determine current pool depth → batch size for new candidates.
        async with self._pool.acquire() as conn:
            pool_size = len(
                await self._proxy_status_repo.list_passing(conn)
            )
        if pool_size < 5:                           # noqa: PLR2004
            new_batch = 20
        elif pool_size < 15:                        # noqa: PLR2004
            new_batch = 15
        else:
            new_batch = 10

        # Step 1: re-verify up to 5 of the oldest passers. Catches
        # entries that died inside the 24 h freshness window before
        # they get served to capture-path consumers.
        async with self._pool.acquire() as conn:
            stale = await self._proxy_status_repo.list_passing_oldest(
                conn, limit=5,
            )
        if stale:
            stale_configs = [ProxyConfig(server=s) for s in stale]
            log.info("worker.gate_reverify_start", count=len(stale))
            passing = await filter_gate_passing(
                stale_configs, concurrency=3,
            )
            async with self._pool.acquire() as conn:
                for proxy in stale_configs:
                    await self._proxy_status_repo.record(
                        conn, proxy.server,
                        gate_passing=proxy in passing,
                    )
            log.info(
                "worker.gate_reverify_done",
                tried=len(stale_configs), still_passing=len(passing),
            )

        # Step 2: probe new candidates.
        await self._gate_probe_batch(batch_size=new_batch)

        # Step 3: evict proxies that have failed the gate N times in a
        # row. Keeps proxy_status from accumulating forever — public
        # SOCKS5 lists churn aggressively and stale failing rows just
        # become noise. (Archives are never deleted; only this kind of
        # throwaway operational state expires.)
        async with self._pool.acquire() as conn:
            evicted = await self._proxy_status_repo.evict_dead(
                conn,
                failure_threshold=(
                    self._settings.proxy_eviction_failure_threshold
                ),
            )
        if evicted > 0:
            log.info("worker.proxy_evicted", count=evicted)

    async def _gate_probe_batch(self, batch_size: int) -> None:
        """Gate-probe up to `batch_size` fresh candidates."""
        if not self._proxy_rotator.proxies:
            return
        # Preserve the healthy-rotator pool; filter for SOCKS5 and
        # consumer-ASN here rather than pre-filtering up front so the
        # generic rotator still benefits from datacenter IPs for
        # non-archive.today captures.
        socks = filter_socks5(list(self._proxy_rotator.proxies))
        if not socks:
            return

        assert self._pool is not None  # noqa: S101
        async with self._pool.acquire() as conn:
            already = set(
                await self._proxy_status_repo.list_passing(
                    conn, max_age_hours=24
                )
            )
        # Skip proxies already confirmed passing recently.
        candidates = [p for p in socks if p.server not in already]
        if not candidates:
            log.debug("worker.gate_probe_no_fresh_candidates")
            return

        consumer = await filter_by_asn(candidates[: batch_size * 5])
        if not consumer:
            # Fall back to ASN-unfiltered candidates. The ASN filter is
            # an optimization (datacenter pass rate ~0%); when the
            # surviving healthy pool is entirely datacenter — common
            # because cloud-hosted SOCKS5s are the most-maintained ones
            # in public lists — skipping the filter is strictly better
            # than producing no probes at all. Most will still fail the
            # gate, but a few may pass, and we self-correct over time.
            log.info(
                "worker.gate_probe_consumer_empty_using_unfiltered",
                candidate_count=len(candidates),
            )
            sample = candidates[:batch_size]
        else:
            sample = consumer[:batch_size]
        log.info("worker.gate_probe_batch_start", count=len(sample))
        passing = await filter_gate_passing(sample, concurrency=3)

        async with self._pool.acquire() as conn:
            for proxy in sample:
                await self._proxy_status_repo.record(
                    conn,
                    proxy.server,
                    gate_passing=proxy in passing,
                )
        log.info(
            "worker.gate_probe_batch_done",
            tried=len(sample),
            passing=len(passing),
        )

    async def _frontend_probe_loop(self) -> None:  # pragma: no cover  # noqa: C901, PLR0912
        """Periodically verify that registered privacy-frontend instances
        actually serve real content (not Anubis/CF challenge pages).

        Longer warmup than _gate_probe_loop because this probe needs a
        gate-passing SOCKS5 to be available first — frontends are
        themselves bot-gated so the probe has to go through the same
        SOCKS5 pool tier-5 uses.
        """
        from urllib.parse import urlparse

        from archiver.privacy_frontends import (
            FRONTENDS,
            discover_instances,
            is_alive_tcp,
            probe_frontend_instance,
        )

        # Event-driven warmup: wait until at least one gate-passing
        # proxy exists, since the frontend probe needs one to reach
        # the instance. Static delays raced gate_probe_loop and left
        # the loop sleeping an hour on cold start.
        while self._running:
            at_proxy = await self._pick_archive_today_proxy()
            if at_proxy is not None:
                break
            await asyncio.sleep(30)
        while self._running:
            at_proxy = await self._pick_archive_today_proxy()
            if at_proxy is None:
                log.debug("worker.frontend_probe.no_gate_passer")
            else:
                for policy in FRONTENDS:
                    if not self._running:
                        return
                    # Pull fresh upstream-registry list (once per pass)
                    # and union with the hardcoded fallback. Registry
                    # outage → empty result → fall through to static.
                    instances = await discover_instances(policy)
                    log.info(
                        "worker.frontend_probe.policy_started",
                        target=policy.target_apex,
                        instance_count=len(instances),
                        static_count=len(policy.instances),
                    )
                    for instance in instances:
                        if not self._running:
                            return
                        # Cheap TCP+TLS pre-filter — drop dead hosts in
                        # ~1 s instead of spinning Camoufox for ~30 s.
                        host = urlparse(instance).hostname or ""
                        alive = await is_alive_tcp(host) if host else False
                        if not alive:
                            log.info(
                                "worker.frontend_probe.unreachable",
                                instance=instance,
                                target=policy.target_apex,
                            )
                            passing = False
                        else:
                            try:
                                passing = await probe_frontend_instance(
                                    policy, instance, at_proxy,
                                )
                            except Exception as exc:  # pragma: no cover
                                log.warning(
                                    "worker.frontend_probe.error",
                                    instance=instance,
                                    error=str(exc)[:120],
                                )
                                passing = False
                        assert self._pool is not None  # noqa: S101
                        async with self._pool.acquire() as conn:
                            await self._frontend_status_repo.record(
                                conn,
                                instance,
                                policy.target_apex,
                                content_verified=passing,
                            )
                        log.info(
                            "worker.frontend_probe.outcome",
                            instance=instance,
                            target=policy.target_apex,
                            passing=passing,
                        )
            # Sleep 1 h between passes — instances don't flip state often.
            for _ in range(3600):
                if not self._running:
                    return
                await asyncio.sleep(1)
