-- Migration 002 — dedup reports per (archive, reporter_ip_hash).
--
-- Before: a single IP could file unlimited reports against the same
-- archive (capped only by the 10/hr per-IP rate limit). Multiple
-- pending rows for one archive_id just clutter the admin queue and
-- give a single bad actor disproportionate visibility weight in any
-- "count reports by archive" query.
--
-- After: a unique index on (archive_id, reporter_ip_hash) when
-- reporter_ip_hash is set. NULL ip_hash (no rate-limit middleware
-- attached) is allowed to coexist — those rows can't be deduped
-- without losing the report entirely.
--
-- Backfill: if dev/prod already has duplicate rows for the same
-- (archive_id, reporter_ip_hash), drop all but the EARLIEST one
-- before applying the constraint. The earliest is the original
-- report; the duplicates are spam.

DELETE FROM reports
WHERE id NOT IN (
    SELECT DISTINCT ON (archive_id, reporter_ip_hash) id
    FROM reports
    WHERE reporter_ip_hash IS NOT NULL
    ORDER BY archive_id, reporter_ip_hash, created_at ASC
)
AND reporter_ip_hash IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_unique_per_ip
    ON reports (archive_id, reporter_ip_hash)
    WHERE reporter_ip_hash IS NOT NULL;
