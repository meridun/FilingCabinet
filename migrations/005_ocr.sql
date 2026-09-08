-- 005_ocr: phase-4 per-page OCR ladder state + full-text search (docs/Architecture.md §5).
-- Additive and forward-only: one new table, one FTS5 index over document.ocr_text, and the
-- triggers that keep them in step. No ALTER and no rewrite of document/occurrence, so
-- re-running `migrate` is a no-op via schema_migrations and phases 2/3 are untouched.

-- One row per page per document: where the ladder got to, and what it read. The ladder is
-- resumable from these rows alone - a page at/above [ocr].min_confidence is never re-read,
-- and a page below it resumes at the rung *after* the one recorded here.
CREATE TABLE page_ocr (
  page_ocr_id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES document(document_id),
  page_number INTEGER NOT NULL,          -- 1-based, page order as rendered
  text        TEXT,
  confidence  REAL NOT NULL DEFAULT 0.0, -- 0.0-1.0, per-page mean
  rung        TEXT,                      -- ladder rung that produced this row
  ocr_source  TEXT,                      -- fine: 'local_text' | 'local_tesseract' | 'vision'
  status      TEXT NOT NULL,             -- 'ok' | 'pending_vision' | 'skipped' | 'exhausted'
  note        TEXT,                      -- machine reason: 'tesseract_missing', 'render_failed',
                                         -- 'rung_unavailable:<name>'
  updated_at  TEXT NOT NULL,
  UNIQUE (document_id, page_number)
);
CREATE INDEX idx_page_ocr_document ON page_ocr(document_id);

-- Full-text index over the rolled-up document text. External content: the text is stored
-- once, in document.ocr_text, and the FTS table holds only the index.
CREATE VIRTUAL TABLE document_fts USING fts5(
  ocr_text,
  content='document',
  content_rowid='document_id'
);

-- Backfill for databases that already hold documents.
INSERT INTO document_fts (rowid, ocr_text)
  SELECT document_id, COALESCE(ocr_text, '') FROM document;

-- The three sync triggers are the only writers of document_fts; no Python module writes it
-- directly, so external-content consistency has exactly one owner.
CREATE TRIGGER document_fts_ai AFTER INSERT ON document BEGIN
  INSERT INTO document_fts (rowid, ocr_text) VALUES (new.document_id, COALESCE(new.ocr_text, ''));
END;

CREATE TRIGGER document_fts_ad AFTER DELETE ON document BEGIN
  INSERT INTO document_fts (document_fts, rowid, ocr_text)
    VALUES ('delete', old.document_id, COALESCE(old.ocr_text, ''));
END;

-- `OF ocr_text ... WHEN old IS NOT new` is load-bearing: ingest's per-file upsert rewrites
-- document.updated_at on every unchanged re-scan, and without this guard every one of those
-- would also rewrite the FTS row.
CREATE TRIGGER document_fts_au AFTER UPDATE OF ocr_text ON document
WHEN old.ocr_text IS NOT new.ocr_text BEGIN
  INSERT INTO document_fts (document_fts, rowid, ocr_text)
    VALUES ('delete', old.document_id, COALESCE(old.ocr_text, ''));
  INSERT INTO document_fts (rowid, ocr_text) VALUES (new.document_id, COALESCE(new.ocr_text, ''));
END;
