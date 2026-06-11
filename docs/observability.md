# Observability

Metrics come from **two** processes with separate registries:

- **Worker** — capture/queue/disk metrics on `:9090/metrics`, bound to
  `127.0.0.1` on the host (front it with a reverse proxy or an SSH
  tunnel if a remote scrape is needed).
- **App (API)** — abuse/admin metrics (`blocklist_hits`,
  `rate_limit_exceeded`, `jobs_queued`, `admin_logins`, `reports`) on
  the app's own `:8000/metrics`. A worker-only scrape misses these.
  The bundled Prometheus already scrapes both (see
  `deploy/prometheus/prometheus.yml`).

The app's `/metrics` lives on the same port as the public site. The
bundled Caddy blocks it at the edge, so in the stock public deployment
it is not internet-reachable. If you run your own proxy instead,
either block `/metrics*` and `/api/metrics*` there too, or set
`ARCHIVER_METRICS_TOKEN` and give your scraper the matching
`Authorization: Bearer` credential.

## Key metrics

| Metric | Type | Source | Notes |
|---|---|---|---|
| `archiver_captures_total{tier,outcome}` | Counter | worker | All capture attempts; `outcome` is `complete`/`failed`/`antibot`. |
| `archiver_capture_duration_seconds{tier}` | Histogram | worker | Time inside `capture_page()`. Buckets at 1/5/10/30/60/120/300 s. |
| `archiver_jobs_queued`, `archiver_jobs_running` | Gauge | app / worker | Queue depth + concurrency utilisation. |
| `archiver_artifacts_dir_bytes_total/_used/_free` | Gauge | worker | Artifact-volume usage. Sampled every 60 s. |
| `archiver_blocklist_hits_total` | Counter | app | Domain blocklist rejections. |
| `archiver_rate_limit_exceeded_total{endpoint}` | Counter | app | API rate-limit rejections. |
| `archiver_admin_logins_total{outcome}` | Counter | app | `success` vs `failure`. |
| `archiver_reports_total{reason}` | Counter | app | Abuse reports by reason code. |

## Required alert rules

Archives are kept forever by design, so the artifact volume grows
monotonically — the operator must be paged *before* it fills.

Add the following to your Prometheus rule file (path varies by
deployment: `/etc/prometheus/rules.d/archiver.yml` for stock
Prometheus, `prometheus.yml`'s `rule_files:` for a single-file setup):

```yaml
groups:
- name: archiver-storage
  rules:
  - alert: ArchiverArtifactVolumeFilling
    expr: |
      (archiver_artifacts_dir_bytes_used
       / archiver_artifacts_dir_bytes_total) > 0.80
    for: 10m
    labels:
      severity: warning
    annotations:
      summary: "Artifact volume {{ $value | humanizePercentage }} full"
      description: |
        The archive artifact volume is past 80 % capacity. Archives
        are immortal by design; this only goes one direction. Provision
        more storage or move older artifacts to a cold tier before the
        worker starts failing writes.
        Current: {{ $value | humanizePercentage }} used.

  - alert: ArchiverArtifactVolumeCritical
    expr: |
      (archiver_artifacts_dir_bytes_used
       / archiver_artifacts_dir_bytes_total) > 0.95
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Artifact volume {{ $value | humanizePercentage }} full — imminent"
      description: |
        Less than 5 % free on the artifact volume. New captures will
        start failing within hours at typical write rates. Add storage
        NOW or pause submission.
```

The 80 % threshold gives a comfortable head start at typical growth
rates (a few GB/day on a busy instance); the 95 % critical fires
immediately for the case where someone missed the warning for a week.

## What NOT to alert on

Don't alert on archive count growth, individual capture failures
(tier escalation handles that), or worker restarts. The system is
designed to be noisy at the per-capture level — the escalation chain
absorbs transient failures, and alerting on each one would burn out
on-call.

## Bundled stack: `--profile monitoring`

The `monitoring` profile starts Prometheus + Grafana with everything
wired up:

```
docker compose --profile monitoring up -d prometheus grafana
```

- **Prometheus** on `http://127.0.0.1:9091` (host-bound to loopback;
  same scrape config + alert rules described above are pre-loaded
  from `deploy/prometheus/`).
- **Grafana** on `http://127.0.0.1:3000`, default `admin/admin`
  (override with `$GRAFANA_ADMIN_PASSWORD`). Prometheus datasource
  is auto-provisioned; the `Archiver` dashboard in the `Archiver`
  folder is auto-loaded.

The dashboard has 10 panels: capture counts (success / failed),
disk-volume gauge with 80/95 % thresholds, queue depth, capture
rate by tier, latency p50/p95 by tier, outcome mix as a percentage,
artifact volume bytes over time, abuse signals (blocklist /
rate-limit / reports), admin login attempts.

To swap in your own Prom/Grafana, skip the profile and copy both
scrape jobs (worker *and* app) from `deploy/prometheus/prometheus.yml`
— remember the app job needs the bearer token if you set
`ARCHIVER_METRICS_TOKEN`.
