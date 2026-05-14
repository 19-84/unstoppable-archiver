# TLS

The public compose overlay (`docker-compose.public.yml`) bundles a
Caddy reverse proxy that terminates TLS automatically via Let's
Encrypt ACME. Out of the box, `make run-public` gives you HTTPS on
443 with no host-side proxy configuration.

## Prerequisites

1. **DNS** — point `$ARCHIVER_PUBLIC_DOMAIN`'s A (and AAAA if you
   have IPv6) records at the public IP of the host running the
   stack. Caddy's ACME HTTP-01 challenge resolves the domain back
   to this machine on port 80; if DNS isn't ready when the stack
   starts, Caddy retries with exponential backoff (you'll see this
   in `docker compose logs caddy`).
2. **Ports** — 80 and 443 must be reachable from the public
   internet. Open the firewall, set up port-forwarding from your
   router, and check there's no other process bound to either
   port on the host.
3. **`.env` vars** — `ARCHIVER_PUBLIC_DOMAIN` and
   `ARCHIVER_ACME_EMAIL` must be set. `scripts/setup-public.sh`
   prompts for both; if you wrote `.env` by hand, see
   `.env.example.public`.

## What Caddy provides

| Capability | Detail |
|---|---|
| Auto-HTTPS | Let's Encrypt cert provisioned on first request, auto-renewed every ~60d |
| HSTS | `max-age=31536000; includeSubDomains; preload` |
| HTTP/3 | UDP/443 enabled, falls back to HTTP/2 + HTTP/1.1 |
| HTTP→HTTPS redirect | All port-80 traffic 301's to port 443 |
| Hardening headers | `X-Content-Type-Options nosniff`, restrictive `Referrer-Policy`, frame-ancestors lockdown |
| Scanner-path 404s | `/wp-admin`, `/.env`, `/.git` etc. blocked at edge |
| Body-size cap | 100 MB |
| Health-checked upstream | Caddy pings `/api/health` every 10s; transient app restarts don't show as 502s |
| Submitter IP | `X-Forwarded-For` passed through; app's `ARCHIVER_TRUSTED_PROXIES=true` consumes it |

Snapshot routes (`/api/archives/{id}/snapshot`) set their own
per-response `Content-Security-Policy: sandbox` so the archive
viewer iframes work; the global header in the Caddyfile only sets
`frame-ancestors`, leaving the snapshot route free to override.

## Customising

`deploy/Caddyfile` is bind-mounted read-only into the container.
For local edits without rebuilding:

```
$EDITOR deploy/Caddyfile
docker compose -f docker-compose.yml -f docker-compose.public.yml restart caddy
```

For more invasive changes (additional sites, dynamic config,
plugins), maintain your own Caddyfile and mount it via a compose
override:

```yaml
# docker-compose.override.yml
services:
  caddy:
    volumes:
      - ./my-Caddyfile:/etc/caddy/Caddyfile:ro
```

## Switching to an external proxy

If you already run Caddy / Traefik / nginx on the host or in front
of this stack, override the bundled Caddy:

```yaml
# docker-compose.override.yml
services:
  caddy:
    profiles:
      - disabled   # keeps it out of `up`
  app:
    ports:
      - "127.0.0.1:8000:8000"  # expose to host for your proxy
```

Then point your external proxy at `127.0.0.1:8000`. Make sure
`X-Forwarded-For` is preserved and `ARCHIVER_TRUSTED_PROXIES=true`
stays set in `.env`.

## ACME troubleshooting

The most common failure mode is a misconfigured DNS or firewall
on first start — symptoms look like Caddy logs repeatedly:

```
{"msg": "challenge failed", "remote_ip": "<acme-server-ip>", ...}
```

Checks, in order:

1. `dig $ARCHIVER_PUBLIC_DOMAIN +short` — does it return your
   public IP?
2. `curl -fsS http://$ARCHIVER_PUBLIC_DOMAIN/` from another host —
   does it reach the Caddy container? (Should redirect to HTTPS;
   that's enough to prove port 80 is open.)
3. `docker compose logs caddy` — what specifically did the ACME
   server complain about?

Let's Encrypt rate-limits failed validation attempts: 5 failures
per account per hostname per hour. If you exhaust it, switch to
the staging endpoint to debug:

```yaml
# in your Caddyfile during debug only
tls {$ARCHIVER_ACME_EMAIL} {
    ca https://acme-staging-v02.api.letsencrypt.org/directory
}
```

Staging certs are issued by an untrusted root (browsers will
complain) but bypass the rate limit. Remove the `ca` line for
production.
