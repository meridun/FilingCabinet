-- 001_init: document identity + file occurrences + move-log.
-- Schema per docs/Architecture.md. Bytes live in the document root (Drive-synced folder);
-- this index is truth for identity (sha256) and metadata only.

-- One row per distinct content hash. Metadata attaches here, not to paths.
CREATE TABLE document (
  document_id   INTEGER PRIMARY KEY,
  sha256        TEXT UNIQUE NOT NULL,
  size_bytes    INTEGER NOT NULL,
  mime          TEXT,
  page_count    INTEGER,
  doc_date      TEXT,                 -- date the document pertains to (from OCR/rules), ISO
  party         TEXT,                 -- vendor / person / institution slug
  doc_type      TEXT,                 -- controlled vocabulary from taxonomy config
  detail        TEXT,
  ocr_text      TEXT,                 -- full extracted text (phase 4)
  ocr_source    TEXT,                 -- local | drive | vision
  first_seen_at TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- One row per path under the root currently holding that content. Several rows per
-- document means exact duplicates on disk (including Drive conflict copies).
CREATE TABLE occurrence (
  occurrence_id INTEGER PRIMARY KEY,
  document_id   INTEGER NOT NULL REFERENCES document(document_id),
  rel_path      TEXT UNIQUE NOT NULL, -- relative to [paths].root, forward slashes
  mtime         REAL NOT NULL,
  size_bytes    INTEGER NOT NULL,
  seen_at       TEXT NOT NULL,        -- last scan that saw this path
  missing_since TEXT                  -- set when a scan no longer finds the path
);
CREATE INDEX idx_occurrence_document ON occurrence(document_id);

-- Every applied rename/move, for undo and audit. Written only by `apply`.
CREATE TABLE move_log (
  move_id     INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES document(document_id),
  plan_id     TEXT NOT NULL,
  from_path   TEXT NOT NULL,
  to_path     TEXT NOT NULL,
  applied_at  TEXT NOT NULL,
  undone_at   TEXT
);
