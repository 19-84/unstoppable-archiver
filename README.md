# Unstoppable Archive

Self-hosted web archiver with multi-tier anti-bot capture. Captures pages
as self-contained HTML (SingleFile) + WARC + screenshot, with automatic
escalation across eight tiers: Playwright Chromium → Camoufox stealth
Firefox → Camoufox-over-proxy → privacy frontends (Scribe, Redlib,
xcancel, …) → Wayback Machine → archive.today → Common Crawl (recent
crawls plus a full-history deep scan back to 2014) → archive.today
submission as the last-resort write.

Two deployment modes:
- **Self-hosted** — single user, local machine, no admin auth required
- **Public** — archive.today-style open submission, admin-moderated takedowns

---

## Self-hosted — run it for yourself

Single user, localhost only, no auth required. Perfect for personal research,
journalism, or preserving links before they rot.

**Setup:**

```bash
git clone https://github.com/19-84/unstoppable-archiver.git
cd unstoppable-archiver
make setup-selfhosted   # copies .env.example.selfhosted → .env
# (optional) edit .env to set a DB password
make run-selfhosted
```

Open <http://localhost:8000>. Paste a URL into the search/submit box and
watch it capture.

**Optional: enable the admin UI** (for takedown/moderation on a shared
machine, e.g. family server):

```bash
# Generate a bcrypt password hash (minimum 8 chars)
docker compose run --rm --no-deps app uv run python scripts/hash_password.py

# Generate a session secret
openssl rand -hex 32

# Add both to .env (paste the literal values — .env files don't run
# shell commands):
#   ARCHIVER_ADMIN_PASSWORD_HASH=$2b$12$...
#   ARCHIVER_SESSION_SECRET=<output of openssl rand>

make run-selfhosted   # restarts with admin enabled
```

Visit `/admin/login` to access moderation features.

**What's off by default:**
- Rate limiting (no bots expected)
- Captcha
- Domain blocklists (you control what you archive)
- robots.txt respect (not implemented; we preserve pages regardless)

---

## Public deployment — run it for others

Open submission service like archive.today: anyone can archive URLs,
admin moderates abuse reports, soft-delete + audit log for accountability.

**Requirements:**
- Linux host with Docker + Docker Compose
- Public domain with DNS pointing at your host (ports 80/443 reachable)

**Setup:**

```bash
git clone https://github.com/19-84/unstoppable-archiver.git
cd unstoppable-archiver
./scripts/setup-public.sh   # interactive: prompts for admin password, generates secrets
```

This creates `.env` with:
- Your public domain + ACME contact email (for the bundled Caddy proxy)
- A bcrypt admin password hash
- Random session secret
- Random strong DB password
- StevenBlack porn blocklist enabled (optional, prompted)

**Start the stack:**

```bash
make run-public
```

This includes a bundled Caddy reverse proxy that auto-provisions
Let's Encrypt TLS for your domain — HTTPS works out of the box, no
host-level proxy needed. To run behind your own external proxy
instead, override the `caddy` service in `docker-compose.public.yml`
(its config lives at `deploy/Caddyfile`).

**Verify:**
- Visit `https://your-domain/` — home page with capture form
- Visit `https://your-domain/admin/login` — admin portal
- Submit a test URL, confirm capture works
- Try a blocked domain from your blocklist, confirm 400

**What's on by default in public mode:**
- Rate limiting per IP (60 submits/hr, 10 reports/hr)
- Domain blocklist (from StevenBlack if enabled during setup)
- Public "Report this archive" form on every archive detail page
- Admin dashboard with moderation queue + audit log
- Soft delete (preserves data for audit defense)
- Submitter IPs hashed on receipt (HMAC-SHA256) — raw IPs are never stored
- HTTPS-only session cookies (requires reverse proxy)

**Optional add-ons:**

Add extra blocklist sources in `.env`:
```
ARCHIVER_BLOCKLIST_URLS=https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn/hosts,https://your-custom-list
ARCHIVER_BLOCKLIST_DOMAINS=specific-bad-site.example,another.test
ARCHIVER_ALLOWLIST_DOMAINS=false-positive.example
```

Enable captcha if you get spammed:
```
# Self-hosted proof-of-work (no third-party, recommended first):
ARCHIVER_CAPTCHA_PROVIDER=altcha
ARCHIVER_ALTCHA_HMAC_KEY=$(openssl rand -hex 32)

# Or hCaptcha (third-party visual, requires signup at hcaptcha.com):
# ARCHIVER_CAPTCHA_PROVIDER=hcaptcha
# ARCHIVER_HCAPTCHA_SITEKEY=...
# ARCHIVER_HCAPTCHA_SECRET=...
```

Restart after env changes: `make stop-public && make run-public`.

---

## Feature matrix

| Feature | Self-hosted | Public |
|---|---|---|
| URL submission (anonymous) | ✓ | ✓ |
| Full-text search | ✓ | ✓ |
| Pagination | ✓ | ✓ |
| Multi-tier capture (Chromium → Camoufox → Proxy → Privacy frontends → Wayback → archive.today → Common Crawl → archive.today submit) | ✓ | ✓ |
| Common Crawl deep-scan fallback (2014–present) | ✓ | ✓ |
| Rotating User-Agent pool (daily refresh, never identifies as archiver) | ✓ | ✓ |
| playwright-stealth + cf_clearance cache | ✓ | ✓ |
| CSP-header stripping for robust SingleFile injection | ✓ | ✓ |
| SingleFile CLI subprocess fallback for strict-CSP sites | ✓ | ✓ |
| Sandboxed snapshot viewer | ✓ | ✓ |
| WARC + screenshot + thumbnail artifacts | ✓ | ✓ |
| Delete button on archive page | ✓ (user) | hidden |
| "Report this archive" button | hidden | ✓ |
| Rate limiting per IP | off (configurable) | on by default |
| Admin dashboard + moderation | optional | required |
| Domain blocklist / allowlist | off (configurable) | on via env var |
| Captcha | off | optional (altcha or hcaptcha) |
| Soft delete with takedown reason | ✓ | ✓ |
| Audit log | ✓ | ✓ |
| Submitter IP handling | hashed on receipt | hashed on receipt |

---

## Operations

**Backup:** `scripts/backup.sh` dumps Postgres + tars the artifacts
directory. Set it up as a daily cron on the host.

**Reload blocklist without restart:** POST `/admin/blocklist/reload`
(requires admin login). Useful when upstream GitHub lists update.

**Upgrade:** `git pull && make run-<mode>` rebuilds images and runs
schema migrations automatically (idempotent `ALTER TABLE IF EXISTS`).

**Health:** `/api/health` (shallow) and `/api/health/deep` (DB connectivity).

---

## Architecture

See `CLAUDE.md` for the full overview. Key components:

- **FastAPI** app (API + htmx-rendered pages)
- **PostgreSQL 17** with tsvector FTS + JSONB audit log
- **Playwright** Chromium + **Camoufox** Firefox in the worker
- **SingleFile** for self-contained HTML snapshots — JS injection first,
  CLI subprocess fallback for strict-CSP sites, `page.content()` as
  ultimate safety net
- **Common Crawl** as the final read-only fallback tier — CDX lookup + WARC
  range-fetch from `data.commoncrawl.org`, with deep-scan of all ~122
  crawls back to 2014 when recent crawls miss
- Rotating **User-Agent pool** refreshed daily from jnrbsn/user-agents
  (never leaks archiver identity)
- **Worker** processes a Postgres-backed job queue with LISTEN/NOTIFY
- **htmx + Tailwind + Jinja2** frontend, all assets vendored locally

---

## License

Public domain via [Unlicense](https://unlicense.org). See `LICENSE`.
Do whatever you want with this.
