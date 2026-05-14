# Backups

The archive is immortal by design — losing it is the worst possible
operational outcome. This doc covers what gets backed up, the
scheduling story, and how to prove restorability *before* you need it.

## What gets backed up

`scripts/backup.sh` produces two artifacts per run, plus sha256
sidecars:

```
/data/backups/db_<TIMESTAMP>.sql.gz                 # gzipped pg_dump
/data/backups/db_<TIMESTAMP>.sql.gz.sha256
/data/backups/artifacts_<TIMESTAMP>.tar.gz          # tarball of /data/archives
/data/backups/artifacts_<TIMESTAMP>.tar.gz.sha256
```

Atomicity: each backup is written as `<file>.tmp`, sha256'd, then
renamed into place. A crash mid-write leaves only the `.tmp` (which
the next run cleans up after an hour). You never see a corrupt-but-
named-final backup.

## Schedule

The compose `backup` service (opt-in via `--profile backup`) runs the
script in a loop, defaulting to once per day:

```
docker compose --profile backup up -d backup
```

Tunables (env on the `backup` service):

| Variable | Default | Notes |
|---|---|---|
| `BACKUP_INTERVAL_SECONDS` | `86400` | 24 h. Pick shorter for higher-churn instances. |
| `BACKUP_RETENTION_DAYS` | `30` | Age-based pruning — keeps backups newer than N days regardless of cadence. |
| `BACKUP_DIR` | `/data/backups` | Mount this on a separate volume from `artifacts`; don't store the safety net on the same disk as the thing you're protecting. |

For production: mount `/data/backups` to a dedicated volume (separate
disk, ideally separate filesystem). Add a daily rsync to offsite
(S3, B2, another physical machine — the choice is policy, not
technology) outside this stack.

## Restore

The restore path verifies integrity before touching the target DB:

```
docker compose exec backup /app/scripts/restore.sh \
    /data/backups/db_20260514_030000.sql.gz \
    /data/backups/artifacts_20260514_030000.tar.gz \
    --target-db postgresql://archiver:.../archiver_restored \
    --target-dir /data/archives
```

The sha256 sidecars are checked first — restore aborts if either
backup's checksum doesn't match. `--target-db` must point at an
EXISTING database that's safe to overwrite (typically a fresh one
you just `CREATE DATABASE`'d for the recovery).

## Verify (the part most teams skip)

A backup you've never restored is not a backup. `verify_backup.sh`
round-trips a backup pair into a scratch database, asserts the schema
and a sentinel query, then drops the scratch DB:

```
export PG_SUPERUSER_URL=postgresql://postgres:...@postgres:5432/postgres
./scripts/verify_backup.sh \
    /data/backups/db_20260514_030000.sql.gz \
    /data/backups/artifacts_20260514_030000.tar.gz
```

The script uses `PG_SUPERUSER_URL` (a role with `CREATE DATABASE`
privilege; usually the postgres superuser) because the archiver
role doesn't have that grant and shouldn't.

**Schedule this** — host cron, weekly, on the newest backup. The
output exit code is 0 on success, non-zero on any failure; wire it
to your alerting layer the same way you'd wire a CI test:

```
# /etc/cron.d/archiver-backup-verify
30 4 * * 0 archiver /opt/archiver/scripts/verify_backup.sh \
    $(ls -t /data/backups/db_*.sql.gz | head -1) \
    $(ls -t /data/backups/artifacts_*.tar.gz | head -1) \
    || curl -fsS -X POST $ALERT_WEBHOOK -d 'backup verify failed'
```

If you don't have alerting, at minimum a weekly email cron telling
you the verify ran. Silence is not success.

## Offsite

Nothing in this repository handles offsite replication — that's
policy your environment owns. The local backup volume protects
against `rm -rf` and disk corruption on the same machine; it does
NOT protect against the machine catching fire or your hosting
provider losing the rack.

Sketch: a host cron line that `rsync`s `/data/backups/` to S3/B2/
remote storage every 24h, after the daily backup completes. Use
restic / borgbackup if you want client-side encryption + dedup. The
sha256 sidecars travel with the files so the offsite copy is also
self-verifying.

## What NOT to back up

- The `proxy_status` table — operational state, refilled from the
  public proxy lists on next worker start.
- The `cf_clearance_cache` — token cookies that expire anyway.
- The `audit_log` IF retention there matters more for compliance
  than recovery; consider streaming to a separate sink.

`pg_dump` includes all of these by default. They're cheap so don't
bother filtering unless backup size becomes painful — at which
point the right answer is exclude tables in the dump command, not
delete rows.
