#!/usr/bin/env bash
# ABOUTME: Backup script for PostgreSQL database and archive artifacts
# ABOUTME: Creates timestamped pg_dump + rsync of artifacts directory
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_URL="${ARCHIVER_DB_URL:?ARCHIVER_DB_URL must be set}"
ARTIFACTS_DIR="${ARCHIVER_ARTIFACTS_DIR:-/data/archives}"

mkdir -p "$BACKUP_DIR"

echo "=== Backup started at $TIMESTAMP ==="

# Database
DB_BACKUP="$BACKUP_DIR/db_$TIMESTAMP.sql.gz"
echo "Dumping database..."
pg_dump "$DB_URL" | gzip > "$DB_BACKUP"
echo "  → $DB_BACKUP ($(du -h "$DB_BACKUP" | cut -f1))"

# Artifacts
ARTIFACTS_BACKUP="$BACKUP_DIR/artifacts_$TIMESTAMP.tar.gz"
echo "Archiving artifacts..."
tar -czf "$ARTIFACTS_BACKUP" -C "$(dirname "$ARTIFACTS_DIR")" "$(basename "$ARTIFACTS_DIR")"
echo "  → $ARTIFACTS_BACKUP ($(du -h "$ARTIFACTS_BACKUP" | cut -f1))"

# Cleanup old backups (keep last 7)
echo "Cleaning old backups (keeping last 7)..."
ls -t "$BACKUP_DIR"/db_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm
ls -t "$BACKUP_DIR"/artifacts_*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm

echo "=== Backup complete ==="
