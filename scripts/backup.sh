#!/usr/bin/env bash
# ABOUTME: Backup script for PostgreSQL database and archive artifacts
# ABOUTME: Atomic writes, sha256 sidecars, age-based retention (not count-based)
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_URL="${ARCHIVER_DB_URL:?ARCHIVER_DB_URL must be set}"
ARTIFACTS_DIR="${ARCHIVER_ARTIFACTS_DIR:-/data/archives}"

mkdir -p "$BACKUP_DIR"

echo "=== Backup started at $TIMESTAMP ==="

# Helper: write to .tmp, sha256 it, then atomic-rename the pair into place.
# A crash mid-write leaves only the .tmp behind, never a half-finished
# real backup that the operator would later believe was good.
atomic_write() {
    local out="$1"
    local tmp="${out}.tmp"
    local sha_out="${out}.sha256"
    # Caller already wrote to "$tmp"; just checksum + rename.
    sha256sum "$tmp" | awk '{print $1}' > "${sha_out}.tmp"
    mv "${sha_out}.tmp" "$sha_out"
    mv "$tmp" "$out"
}

# Database — pipe pg_dump through gzip into the .tmp, then promote.
DB_BACKUP="$BACKUP_DIR/db_$TIMESTAMP.sql.gz"
echo "Dumping database..."
pg_dump "$DB_URL" | gzip > "${DB_BACKUP}.tmp"
atomic_write "$DB_BACKUP"
echo "  → $DB_BACKUP ($(du -h "$DB_BACKUP" | cut -f1))"

# Artifacts — tar+gzip into .tmp, then promote.
ARTIFACTS_BACKUP="$BACKUP_DIR/artifacts_$TIMESTAMP.tar.gz"
echo "Archiving artifacts..."
tar -czf "${ARTIFACTS_BACKUP}.tmp" \
    -C "$(dirname "$ARTIFACTS_DIR")" \
    "$(basename "$ARTIFACTS_DIR")"
atomic_write "$ARTIFACTS_BACKUP"
echo "  → $ARTIFACTS_BACKUP ($(du -h "$ARTIFACTS_BACKUP" | cut -f1))"

# Age-based retention. Count-based ("keep last 7") collapses to hours
# under an hourly schedule and to weeks under weekly — neither is what
# a multi-year archive wants. Keep N days regardless of cadence.
echo "Pruning backups older than ${RETENTION_DAYS}d..."
find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'db_*.sql.gz' -o -name 'db_*.sql.gz.sha256' \
       -o -name 'artifacts_*.tar.gz' -o -name 'artifacts_*.tar.gz.sha256' \) \
    -mtime "+${RETENTION_DAYS}" -print -delete

# Clean any leftover .tmp from a previous interrupted run.
find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tmp' -mmin +60 -print -delete

echo "=== Backup complete ==="
