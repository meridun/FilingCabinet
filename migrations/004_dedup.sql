-- 004_dedup: phase-3 dedup storage (docs/Architecture.md §4). Additive and forward-only:
-- three CREATE TABLEs plus one index, no ALTER and no rewrite of document/occurrence, so
-- re-running `migrate` is a no-op via schema_migrations.

-- Page-level perceptual hashes, one row per page. Feeds the near-duplicate and subset
-- tiers; the exact tier needs none of it (document.sha256 already decides identity).
CREATE TABLE page_hash (
  document_id INTEGER NOT NULL REFERENCES document(document_id),
  page_no     INTEGER NOT NULL,        -- 0-based, page order as rendered
  phash       TEXT NOT NULL,           -- hex digest, algo-defined width
  algo        TEXT NOT NULL,           -- 'phash8' = ImageHash phash, 8x8 DCT, 64 bits
  computed_at TEXT NOT NULL,
  PRIMARY KEY (document_id, page_no)
);
-- Exact-phash prefilter: candidate pairs for the near tier without an all-pairs scan.
CREATE INDEX idx_page_hash_phash ON page_hash(phash);

-- Uncertain matches (near and subset tiers) awaiting a human verdict. Nothing here is
-- ever auto-merged: tools never move or rename documents unasked (docs/Architecture.md §6).
CREATE TABLE dupe_review (
  review_id   INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,           -- 'near' | 'subset'
  document_a  INTEGER NOT NULL REFERENCES document(document_id),
  document_b  INTEGER NOT NULL REFERENCES document(document_id),
  score       REAL,                    -- near: mean page distance; subset: matched fraction
  detail      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'dup' | 'not_dup'
  created_at  TEXT NOT NULL,
  resolved_at TEXT,
  -- 'near' pairs are stored normalized (document_a < document_b); for 'subset' the order
  -- is meaningful (A is contained in B) and is stored as found.
  UNIQUE (kind, document_a, document_b)
);

-- The labelled sample `dupes label` builds from the real corpus, used to tune
-- [dedup].phash_max_distance (and, once phase 4 lands, the shingle threshold).
CREATE TABLE dupe_label (
  label_id    INTEGER PRIMARY KEY,
  document_a  INTEGER NOT NULL,
  document_b  INTEGER NOT NULL,
  kind        TEXT NOT NULL,           -- 'near' | 'subset' | 'exact'
  verdict     TEXT NOT NULL,           -- 'dup' | 'not_dup'
  source      TEXT NOT NULL,           -- who judged: 'cli', 'import', ...
  labelled_at TEXT NOT NULL,
  UNIQUE (document_a, document_b, kind)
);
