# Architecture

Hub doc. Decisions here were fixed in the 2026-09-07 design interview; subsystem pages
(`Architecture_<Subsystem>.md`) split off as phases land and this page grows past a map.

## 1. Two truths

| Concern | Source of truth | Notes |
|---|---|---|
| Document bytes | The Drive-synced local folder (`[paths].root`) | Google Drive's client replicates it. FilingCabinet reads it and writes to it only on an approved `apply`. No Drive API in v1. |
| Identity and metadata | SQLite index on a non-synced local path (`[paths].data_dir`) | WAL mode. Cloud sync corrupts `-wal`/`-shm` sidecars, so only `VACUUM INTO` snapshots go to `[paths].snapshot_dir`. |

**The document ID is the sha256 of the file bytes.** Filenames are derived, renameable labels.
This is what makes dedup free, makes Drive conflict copies (`foo (1).pdf`, "conflicted copy")
detectable as the same document, and gives other programs (pemr) a stable key to reference
without depending on paths or on this schema.

## 2. Schema (migration `001_init.sql`)

- `document` — one row per distinct hash; all metadata (date, party, type, OCR text) attaches
  here, never to a path.
- `occurrence` — one row per path under the root currently holding that content. Several
  occurrences of one document are exact duplicates on disk.
- `move_log` — every applied rename/move with `plan_id`, for undo and audit. Written only by
  `apply`.
- `schema_migrations` — forward-only numbered `.sql` files in `migrations/`.

Later phases add: page-level hashes (dedup), FTS5 over `ocr_text` (search), a rules table or
config-loaded rule set (classification), a review-queue table (uncertain dupes and proposals).

## 3. Ingest (phase 2)

`filingcabinet ingest` walks the root, hashes new or changed files (skip when `mtime` and size
match the last occurrence row), records page count and mime, and marks vanished paths
`missing_since` rather than deleting rows. Drive conflict-copy filename patterns are flagged.
Incremental and resumable; safe to run on a schedule.

Migration `002_ingest.sql` adds the two columns ingest needs: `occurrence.conflict_kind`
(NULL = ordinary path, `drive_numbered` for `foo (1).pdf`, `drive_conflicted_copy` for a
"conflicted copy" filename) and `occurrence.hashed_at` (when the current `document_id` binding
was computed). Migration `003_scan.sql` adds the `scan` table (one AUTOINCREMENT row per
ingest run) and `occurrence.last_scan_id`: the missing sweep orders by that monotonic token
rather than by wall-clock time, which on a coarse system clock can tie between runs.
Candidate extensions and exclude patterns default in `filingcabinet/ingest.py`
and are overridable via `[ingest]` in `config.toml`. `ingest --json` reports scanned / new /
changed / unchanged / missing / error counts.

## 4. Dedup (phase 3)

Three tiers, each feeding a `dupes report`:

1. **Exact** — same sha256, several occurrences. Zero false positives.
2. **Near-duplicate** — the same physical pages scanned twice differ byte-wise. Page-level
   perceptual hash (Hamming distance under `[dedup].phash_max_distance`) plus OCR-text shingle
   similarity once OCR exists. Matches land in a review queue, not an auto-merge.
3. **Subset** — every page hash of A appears in B (a 3-page scan inside a 10-page scan).

Thresholds need a labelled sample from the real corpus; `dupes label` builds it.

Migration `004_dedup.sql` adds the three tables this runs on: `page_hash` (per-page perceptual
hash), `dupe_review` (the queue — `status` is `pending`, `dup`, or `not_dup`; nothing is ever
auto-merged), and `dupe_label` (the labelled sample). The perceptual-hash stack is the optional
`dedup` extra: without it `dupes report` still reports the exact tier and says
`phash_available: false`.

On a large corpus the near/subset passes stop at a candidate-pair budget and report
`truncated: true` in `--json` — a truncated run is a partial scan of tiers 2-3, not a clean bill
of health; re-run or narrow the corpus if that matters for a given pass.

## 5. OCR (phase 4)

A ladder, configured in `[ocr].ladder`, walked per page until confidence clears
`[ocr].min_confidence`:

1. **local** — PyMuPDF for embedded text layers, tesseract (via ocrmypdf or direct) for
   rasterized pages. Free, private, batchable.
2. **drive** — Google Drive's own text layer via the Drive API. Deferred until a Drive-API
   phase exists.
3. **vision** — the agent reads the page image and supplies text; the tool validates and
   commits with `ocr_source = 'vision'`. Last resort, costs tokens.

Text is stored per document, FTS5-indexed, queried by `find`. Per-page confidence is recorded
so the ladder is resumable.

## 6. Organize (phases 5-6)

**Rules first, agent for the remainder.** A taxonomy config (doc types, party aliases,
regex/keyword rules) classifies known vendors deterministically. Unknowns go to the agent, whose
verdicts carry provenance (`agent`) and can be promoted into rules.

`propose` writes a **plan**: per document, the target name from `[naming].template` (default
`{doc_date}_{party}_{doc_type}_{detail}`), any folder move, tags, and the rule or agent that
decided. A plan is a file a human reviews. `apply <plan>` executes it, writes `move_log`, and
checks mtime stability and file locks first so it never fights the sync client mid-write.
`undo <plan_id>` reverses from the log.

The date in a filename is the date the document pertains to, from OCR or rules. Scan date
stays in the index.

**Tools never move or rename files unasked.** This is the invariant every change is measured
against.

## 7. Agent surface (phase 7)

Every CLI verb takes `--json`. A thin MCP server wraps the verbs as typed tools: `ingest`,
`status`, `find`, `dupes`, `propose`, `apply`, `undo`. The engine and CLI need none of the MCP
dependency; `pip install filingcabinet[mcp]` adds it.

## 8. Instance model and privacy

- **Framework repo (this one):** public-capable. `.gitignore` blocks document formats and
  databases; `npm run check:docs` fails CI on any tracked offender.
- **Instance repo (per user, private):** `config.toml`, taxonomy and rules, index snapshots,
  the move-log export. Documents stay in the Drive folder; Drive is their backup. This
  deliberately diverges from pemr-data, which commits a content-addressed copy of every scan.
  Scaffold one with `filingcabinet instance init <dir>` - see
  [Development_Instance.md](Development_Instance.md).
- **pemr:** independent. Both projects use sha256 content IDs, so a later bridge (pemr
  referencing a FilingCabinet document) is a lookup, not a dependency. No shared code or DB.

## 9. Open questions

- Local-to-Drive drift that has not synced down as a file needs the Drive API. Deferred.
- Windows OCR toolchain (tesseract, Ghostscript) install and a `doctor` verb. See
  [Development.md](Development.md).
- graphify over OCR text exports for cross-document entity linking. Experiment after phase 4.
