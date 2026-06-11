# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public issue.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
("Report a vulnerability" under the repository's **Security** tab).

Include a description, reproduction steps, affected version/commit, and impact.
You can expect an acknowledgement within 7 days and a status update within 30.

## Supported versions

This project has not yet cut a stable release. Security fixes land on `main`;
run the latest `main` for the most current fixes.

## Scope and threat model

Unstoppable Archive is **self-hosted infrastructure**. The most relevant
concerns:

- **SSRF** — submitted URLs are fetched server-side. Inputs are validated
  (`url_safety.py`: scheme allowlist, private/loopback/link-local IP blocking,
  Docker-internal hostname blocking). Report any bypass.
- **Credential handling** — the admin password is stored only as a bcrypt hash;
  DB credentials, session secret, and admin hash are injected at runtime via
  env vars/secrets, never baked into images. Report any leak path.
- **Stored content** — captured pages are attacker-influenced HTML. They are
  served from the viewer; report stored-XSS or sandbox-escape vectors.
- **Destructive API** — admin actions require admin auth; `DELETE` requires
  `ARCHIVER_API_KEY` when set. The public deployment generates and requires
  the key (`setup-public.sh` + compose enforcement); self-hosted instances
  may leave it empty, which disables the check on the assumption that the
  app is reachable only from localhost. Report any auth bypass.

Out of scope: the behavior of upstream services the archiver fetches from
(Wayback, archive.today, Common Crawl, privacy frontends), and denial-of-service
from intentionally pointing your own instance at hostile targets.

## Dependencies

Python dependencies are pinned via `uv.lock`. Run an audit with:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm app \
    sh -c "uv pip install pip-audit -q && uv run pip-audit"
```
