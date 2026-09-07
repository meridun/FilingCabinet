-- 003_scan: give each ingest run a monotonic identity so the missing sweep never ties.
-- Wall-clock timestamps are not a safe ordering: the Windows system clock ticks every
-- ~0.5-16 ms, so two runs can share a timestamp and `seen_at < scan_started_at` sweeps
-- nothing. Forward-only, additive.

-- One row per ingest run. scan_id is the monotonic token stamped on every occurrence
-- the run sees; started_at is for humans only.
CREATE TABLE scan (
  scan_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL
);

-- The newest scan that saw this path. NULL for rows written before 003.
ALTER TABLE occurrence ADD COLUMN last_scan_id INTEGER;
