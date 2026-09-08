-- 006_organize: phase-5 agent classification verdicts (docs/Architecture.md §6).
-- Additive and forward-only: one new table, no ALTER and no rewrite of document/occurrence,
-- so re-running `migrate` is a no-op via schema_migrations and phases 2-4 are untouched.

-- Agent verdicts ONLY. A rule match is a pure function of the instance's taxonomy file, so
-- persisting one would go stale the moment the rule is edited; `propose` recomputes rule
-- matches on every run and stores nothing. document.doc_date/party/doc_type/detail stay NULL
-- until `apply` (phase 6) commits an approved plan - `propose` never writes them.
CREATE TABLE classification (
  classification_id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES document(document_id),
  party       TEXT,
  doc_type    TEXT,
  detail      TEXT,
  doc_date    TEXT,                 -- ISO YYYY-MM-DD
  tags        TEXT,                 -- JSON array text
  folder      TEXT,                 -- proposed folder, relative to [paths].root
  provenance  TEXT NOT NULL,        -- 'agent' (rule matches are recomputed, never stored)
  note        TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (document_id)
);
CREATE INDEX idx_classification_document ON classification(document_id);
