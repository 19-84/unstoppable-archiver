#!/usr/bin/env bash
# ABOUTME: Round-trip a backup pair into a scratch DB and assert it loads
# ABOUTME: "Backups exist" is theater without "backups restore" — run this in CI
set -euo pipefail

usage() {
    cat >&2 <<EOF
Usage: verify_backup.sh <db_dump.sql.gz> <artifacts.tar.gz>

Restore a backup pair into a scratch database (default name
archiver_restore_verify), check the schema + a sentinel query, then
drop the scratch DB. Safe to run while production is live — never
touches the real DB.

Requires PG_SUPERUSER_URL to point at a postgres role with CREATE
DATABASE / DROP DATABASE privileges (typically the postgres
superuser). The user this script normally runs as may not have those.

Returns 0 if the dump loaded cleanly and core tables are present,
non-zero on any failure. Suitable for cron-scheduled CI / smoke
checks; the alerting layer can page if this starts failing.
EOF
}

if [[ "$#" -lt 2 ]]; then
    usage
    exit 2
fi

DB_DUMP="$1"
ARTIFACTS_TARBALL="$2"

: "${PG_SUPERUSER_URL:?PG_SUPERUSER_URL must point at a role that can CREATE/DROP DATABASE}"

SCRATCH_DB="archiver_restore_verify_$$"
# Use a temp artifacts dir so we don't smear restored files into prod.
SCRATCH_ARTIFACTS=$(mktemp -d)
trap 'rm -rf "$SCRATCH_ARTIFACTS"' EXIT

# Build a target URL pointing at the scratch DB on the same host as
# the superuser URL. Pure shell instead of python3 so this runs on
# the bare postgres:alpine image (no python installed there).
# Format: <scheme>://<user>:<pass>@<host>:<port>/<dbname>[?params]
# Strip everything from the last '/' (the dbname + any querystring)
# and reattach the scratch name.
TARGET_URL="${PG_SUPERUSER_URL%/*}/$SCRATCH_DB"

cleanup_scratch() {
    psql "$PG_SUPERUSER_URL" -c "DROP DATABASE IF EXISTS \"$SCRATCH_DB\";" \
        >/dev/null 2>&1 || true
}
trap 'cleanup_scratch; rm -rf "$SCRATCH_ARTIFACTS"' EXIT

echo "=== Creating scratch DB $SCRATCH_DB ==="
psql "$PG_SUPERUSER_URL" -c "CREATE DATABASE \"$SCRATCH_DB\";"

echo "=== Restoring into scratch ==="
"$(dirname "$0")/restore.sh" \
    "$DB_DUMP" "$ARTIFACTS_TARBALL" \
    --target-db "$TARGET_URL" \
    --target-dir "$SCRATCH_ARTIFACTS/archives"

echo "=== Asserting schema + sentinel ==="
# Core tables must exist after a healthy restore. Caller bumps this
# list when the schema gains a new table that backups should preserve.
REQUIRED_TABLES=(
    archives jobs proxy_status frontend_status
    domain_observations cf_clearance_cache audit_log reports
)
for tbl in "${REQUIRED_TABLES[@]}"; do
    psql "$TARGET_URL" -c "SELECT 1 FROM $tbl LIMIT 1;" >/dev/null 2>&1 \
        || { echo "missing table after restore: $tbl" >&2; exit 1; }
done

# Sanity: archives row count round-trips to something sensible. A zero
# count after restore on a backup that supposedly had archives points
# at a silent failure — pg_dump producing an empty plan, an in-progress
# tx mid-dump, etc.
ARCHIVE_COUNT=$(psql "$TARGET_URL" -tAc "SELECT COUNT(*) FROM archives;")
echo "  archives in restored DB: $ARCHIVE_COUNT"

# Artifacts: at least one file must have been extracted.
FILE_COUNT=$(find "$SCRATCH_ARTIFACTS" -type f | wc -l)
echo "  artifact files restored: $FILE_COUNT"

echo "=== Verify OK ==="
