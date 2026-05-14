#!/usr/bin/env bash
# ABOUTME: Restore from a backup pair produced by backup.sh
# ABOUTME: Verifies sha256 sidecars before touching the target database
set -euo pipefail

usage() {
    cat >&2 <<EOF
Usage: restore.sh <db_dump.sql.gz> <artifacts.tar.gz> [--target-db URL] [--target-dir PATH]

Restore a database dump + artifacts tarball produced by backup.sh.

Arguments:
  db_dump.sql.gz     Path to the gzipped pg_dump file.
  artifacts.tar.gz   Path to the tarball of the artifacts directory.

Options:
  --target-db URL    Postgres URL to restore INTO (default: \$ARCHIVER_DB_URL).
                     The target database must exist and SHOULD be empty —
                     pg_restore will error out on conflicts otherwise. Use a
                     scratch DB (e.g. archiver_restore_test) for verification,
                     or DROP DATABASE + CREATE before pointing at production.
  --target-dir PATH  Where to extract the artifacts (default:
                     \$ARCHIVER_ARTIFACTS_DIR or /data/archives).

Both sidecar files (<backup>.sha256) must be present and match — the
restore aborts early on mismatch rather than touching the target on a
corrupt backup.
EOF
}

if [[ "$#" -lt 2 ]]; then
    usage
    exit 2
fi

DB_DUMP="$1"
ARTIFACTS_TARBALL="$2"
shift 2

TARGET_DB="${ARCHIVER_DB_URL:-}"
TARGET_DIR="${ARCHIVER_ARTIFACTS_DIR:-/data/archives}"

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --target-db)   TARGET_DB="$2"; shift 2 ;;
        --target-dir)  TARGET_DIR="$2"; shift 2 ;;
        *)             usage; exit 2 ;;
    esac
done

if [[ -z "$TARGET_DB" ]]; then
    echo "no target DB — pass --target-db or set ARCHIVER_DB_URL" >&2
    exit 2
fi

# Refuse to run unless both sha256 sidecars are present + match.
# A backup without integrity proof is not a restore source; treating
# it as one risks silently importing corruption.
verify_sha() {
    local f="$1"
    local sidecar="${f}.sha256"
    if [[ ! -f "$sidecar" ]]; then
        echo "missing sidecar: $sidecar" >&2
        return 1
    fi
    local recorded actual
    recorded=$(cat "$sidecar")
    actual=$(sha256sum "$f" | awk '{print $1}')
    if [[ "$recorded" != "$actual" ]]; then
        echo "sha256 mismatch on $f" >&2
        echo "  recorded: $recorded" >&2
        echo "  actual:   $actual" >&2
        return 1
    fi
    echo "  ✓ $f matches $sidecar"
}

echo "=== Verifying backup integrity ==="
verify_sha "$DB_DUMP"
verify_sha "$ARTIFACTS_TARBALL"

echo "=== Restoring database to $TARGET_DB ==="
gunzip -c "$DB_DUMP" | psql "$TARGET_DB"

echo "=== Restoring artifacts to $TARGET_DIR ==="
mkdir -p "$TARGET_DIR"
# tarball contains an `<artifacts_basename>/...` top level; extract one
# directory up so paths resolve to TARGET_DIR/<archive>/<timestamp>/...
PARENT=$(dirname "$TARGET_DIR")
mkdir -p "$PARENT"
tar -xzf "$ARTIFACTS_TARBALL" -C "$PARENT"

echo "=== Restore complete ==="
