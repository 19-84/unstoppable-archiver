# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First public-ready state. Pre-1.0: interfaces may change.

### Added

- Multi-tier capture pipeline with automatic escalation: Playwright Chromium →
  Camoufox stealth Firefox → Camoufox-over-proxy → Wayback Machine →
  archive.today → Common Crawl.
- `PRIVACY_FRONTEND` tier — route eligible URLs through Scribe / Redlib /
  xcancel etc., gated by content-positive probing per `(instance, apex)`.
- `ARCHIVE_TODAY_SUBMIT` tier — last-resort write to archive.today through a
  gate-passing SOCKS5 pool.
- Multi-format capture: self-contained SingleFile HTML, WARC, screenshot,
  thumbnail.
- FastAPI app with htmx + Jinja2 + Tailwind UI; PostgreSQL with tsvector FTS
  and a LISTEN/NOTIFY job queue.
- Self-hosted and public deployment modes; optional Tor / I2P routing for
  darknet URLs.
- Rotating User-Agent pool, CSP stripping for SingleFile, cookie-consent
  handling, bookmarklet, Prometheus metrics.

### Security

- SSRF protection on submitted URLs (scheme allowlist, private/loopback IP and
  Docker-internal hostname blocking).
- Admin auth via bcrypt; secrets injected at runtime, never baked into images.
- Patched 11 known CVEs in transitive dependencies (aiohttp, urllib3, starlette,
  lxml, idna, python-multipart).

[Unreleased]: https://github.com/19-84/unstoppable-archiver/commits/main
