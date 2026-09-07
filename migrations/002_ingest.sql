-- 002_ingest: columns phase-2 `ingest` needs on top of 001_init. Additive only
-- (SQLite applies ADD COLUMN without a table rewrite); forward-only, no down migration.

-- Drive conflict-copy flag. NULL = ordinary path; 'drive_numbered' = "foo (1).pdf";
-- 'drive_conflicted_copy' = filename containing "conflicted copy" (case-insensitive).
ALTER TABLE occurrence ADD COLUMN conflict_kind TEXT;

-- When the current document_id binding was computed, so a later phase can audit
-- staleness and the incremental skip path is observable.
ALTER TABLE occurrence ADD COLUMN hashed_at TEXT;
