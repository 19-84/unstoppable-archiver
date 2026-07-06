-- Original snapshot time for captures served from historical sources
-- (wayback / archive.today / commoncrawl). completed_at records when WE
-- stored the archive; for a CC record crawled in 2015 that is off by a
-- decade. Previously this was only recoverable by parsing the 14-digit
-- stamp out of metadata->>'source_url'. NULL for direct captures.
ALTER TABLE archives ADD COLUMN IF NOT EXISTS snapshot_timestamp TIMESTAMPTZ;

COMMENT ON COLUMN archives.snapshot_timestamp IS
    'Upstream snapshot/crawl time for historical-source captures; NULL for direct captures';
