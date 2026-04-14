# ABOUTME: VSDD spec for the job worker process
# ABOUTME: Defines behavioral contract for job claiming, capture orchestration, and tier escalation

# Worker Process Specification

## 1. Behavioral Description

The worker is a standalone async process that:
- Listens for `new_job` PostgreSQL notifications via LISTEN/NOTIFY
- Falls back to polling every 5 seconds if no notification received
- Claims the next queued job atomically via `SELECT FOR UPDATE SKIP LOCKED`
- Runs the capture pipeline for the job's URL and tier
- On success: saves artifacts, updates archive to `complete`
- On anti-bot detection: re-queues at the next escalation tier
- On capture error with retries remaining: re-queues with `retry`
- On capture error with retries exhausted: marks archive as `failed`
- Supports concurrent captures bounded by a semaphore
- Shuts down gracefully on SIGINT/SIGTERM

## 2. Interfaces

```python
class Worker:
    def __init__(self, settings: Settings) -> None: ...
    async def run(self) -> None: ...       # Main loop
    async def shutdown(self) -> None: ...  # Graceful stop
```

Entry point: `python -m archiver` runs `Worker.run()`.

## 3. Pre/Postconditions

### run()
- PRE: PostgreSQL is reachable, schema is initialized
- POST: Worker loops until shutdown() is called or SIGTERM received

### _process_job(job)
- PRE: job.status == RUNNING, job.locked_by == self.worker_id
- POST(success): archive.status == COMPLETE, job.status == COMPLETE, artifacts saved
- POST(anti-bot, tier < max): new job enqueued at next tier
- POST(error, retries left): new job enqueued with retry
- POST(error, no retries): archive.status == FAILED, job.status == FAILED

## 4. Edge Cases

- Worker starts with stale locked jobs in DB → reclaim them on startup
- PostgreSQL connection drops mid-processing → job stays locked, reclaimed after timeout
- Capture timeout → treated as CaptureError with retry
- Browser crash → CaptureError, browser pool re-creates on next use
- Concurrent workers claim different jobs (SKIP LOCKED guarantees no double-claim)
- All 5 tiers exhausted → archive marked FAILED
- SIGTERM during capture → wait for in-flight jobs to complete (bounded timeout)

## 5. Verification Architecture

- Unit tests: mock capture_page, test state transitions
- Integration tests: real PostgreSQL, mock browser
- Property tests: for any sequence of job outcomes, archive/job states are consistent

## 6. Purity Boundary

- Worker is integration-tier (I/O: database, browser, filesystem)
- Uses beartype + icontract, not crosshair
- Tier escalation logic is pure and can be unit tested separately
