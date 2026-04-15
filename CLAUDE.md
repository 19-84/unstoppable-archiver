# Unstoppable Archive

Self-hosted web archiver with multi-tier anti-bot capture. Captures web pages in multiple formats (SingleFile HTML + WARC + screenshot) with automatic escalation through Playwright Chromium, Camoufox stealth Firefox, proxy rotation, and public archive fallbacks.

## Development

All development happens inside Docker containers.

```bash
make dev          # Start dev environment (hot reload)
make test         # Run tests (excludes slow/integration)
make test-all     # Run all tests including slow
make lint         # Run ruff linter
make typecheck    # Run pyright strict
make fmt          # Auto-format with ruff
make all          # lint + typecheck + test
```

## Project Structure

- `src/archiver/` — Application source (src layout)
- `tests/` — Test suite (pytest + hypothesis)
- `src/archiver/vendor/` — Vendored SingleFile JS bundle

## Tech Stack

- **Python 3.12** with beartype + icontract contracts
- **FastAPI** async API server
- **PostgreSQL** with tsvector FTS + LISTEN/NOTIFY job queue
- **Playwright** + **Camoufox** for browser automation
- **SingleFile** JS injection for self-contained HTML snapshots
- **warcio** for WARC file writing
- **htmx + Jinja2 + Tailwind CSS** frontend

## Quality Gates

- 95% test coverage minimum
- pyright strict mode, zero errors
- ruff clean (E,W,F,UP,B,SIM,I,N,C4,RUF,S)
- beartype on all public functions
- icontract @require/@ensure on functions with complexity > 1
- Conventional commits enforced via pre-commit

## Database

PostgreSQL 17. Connection via `ARCHIVER_DB_URL` env var.
Schema managed in `src/archiver/db.py`.

## Docker Services

- `app` — FastAPI server (port 8000)
- `worker` — Capture job processor
- `postgres` — PostgreSQL 17
- `tor` — Tor SOCKS5 proxy (profile: darknet)
- `i2p` — I2P HTTP proxy (profile: darknet)

## Production Deployment

Required environment variables:
- `ARCHIVER_DB_URL` — PostgreSQL connection string (use strong password)
- `ARCHIVER_API_KEY` — API key for destructive operations (DELETE)

Optional:
- `ARCHIVER_LOG_LEVEL` — default INFO
- `ARCHIVER_LOG_FORMAT` — json (production) or console (dev)
- `ARCHIVER_MAX_CONCURRENT_CAPTURES` — default 2
- `ARCHIVER_PROXY_LIST` — comma-separated proxy URLs for Tier 3

Backup: `scripts/backup.sh` (requires `pg_dump` and `ARCHIVER_DB_URL`)

All assets (CSS, JS, fonts) are vendored locally — no external CDN dependencies.
