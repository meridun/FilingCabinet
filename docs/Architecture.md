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

Later phases add: page-level hashes (dedup), FTS5 over `ocr_text` (search), a `classification`
table for agent verdicts (rule matches are recomputed from the taxonomy config, never stored —
§6), a review-queue table (uncertain dupes and proposals).

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

A ladder, configured in `[ocr].ladder` (default `["local", "vision"]`), walked per page until
confidence clears `[ocr].min_confidence`:

1. **local** — PyMuPDF reads embedded text layers directly; tesseract (invoked directly, no
   ocrmypdf/Ghostscript) runs on rasterized pages, rendered into an OS temp dir. Free, private,
   batchable.
2. **drive** — Google Drive's own text layer via the Drive API. Reserved in config, not
   implemented; a listed-but-unimplemented rung is skipped with a recorded reason rather than
   erroring. Deferred until a Drive-API phase exists.
3. **vision** — the agent reads the page image and supplies text via `filingcabinet ocr submit`;
   the tool validates and commits with `ocr_source = 'vision'` at confidence `1.0`. Last resort,
   costs tokens; never run automatically by `ocr run` — a page needing it is left `pending_vision`
   for the agent to close out.

Migration `005_ocr.sql` adds `page_ocr` (one row per page: `confidence`, `rung`, `ocr_source`,
`status` — `ok | pending_vision | skipped | exhausted`, `note` for the machine-readable reason)
and `document_fts`, an external-content FTS5 index over `document.ocr_text` kept in sync by three
triggers (insert/delete/update-of-`ocr_text`) that are its only writers. `document.ocr_source`
keeps the coarse `local | drive | vision` vocabulary from §2; `page_ocr.ocr_source` is the finer
`local_text | local_tesseract | vision`. `filingcabinet find <query> [--json]` queries the index;
`filingcabinet doctor` reports tesseract presence/version (and PyMuPDF's) without raising when
absent.

Per-page confidence is persisted so a re-run is resumable: a page already at or above
`min_confidence` is skipped. A page below threshold normally resumes at the rung *after* the one
last recorded (strictly advancing, terminating in `exhausted`) — except a rung that was
*unavailable* in the environment (tesseract missing, or an unimplemented rung name) resumes *at*
that rung, so installing tesseract and re-running actually picks the page back up. Either way, a
page carrying text is never blanked by a later pass that reads less (a re-escalation preserves the
prior text/confidence/source until something better replaces it), so raising `min_confidence`
cannot silently drop a document out of `find`. `ocr run --json` reports a `degraded` count —
pages currently deferred for an environment reason — alongside the status counts.

## 6. Organize (phases 5-6)

**Rules first, agent for the remainder.** An instance-side `taxonomy.toml` (never committed to
this repo — §9) holds doc types, parties (`display` name plus `aliases`), and rules. A rule names
an `id`, optional `party` / `doc_type` / `folder` / `tags`, and match terms (`all` — every term
must appear; `any` — at least one must appear, when non-empty; `none` — none may appear;
`regex` — optional, searched case-insensitively). Rules are tried in `(priority descending, id
ascending)` order — a total order, so a plan is reproducible — and the first match wins. A rule
with none of `all` / `any` / `regex` matches nothing on purpose: a catch-all would silently claim
every document. A document no rule matches is `unclassified` and waits for an agent verdict,
submitted through `filingcabinet classify` and persisted in the `classification` table
(migration `006_organize.sql`, one row per document, `provenance='agent'`). **Rule matches are
never persisted** — they are a pure function of `taxonomy.toml` recomputed on every `propose`, so
an edited rule takes effect immediately without a stale stored copy; a stored agent verdict
outranks a competing rule. `classify` also prints a paste-ready `[[rules]]` stanza so a good
verdict can be promoted into a rule — the tool never edits `taxonomy.toml` itself, the same
never-act-unasked posture as document moves.

`propose` writes a **plan file** (JSON, one per run, named `plan-<timestamp>-<hex>.json`) to
`[paths].plan_dir`, which must resolve outside `[paths].root` — a plan is a proposal, not a
document, and `propose` refuses to start if the configured or `--out` path would land inside the
root. The plan is `{plan_version, plan_id, created_at, root, taxonomy, template, summary,
entries[]}`. Each entry carries, per document: `current_path`, `target_path`, `folder`,
`target_name`, `fields` (the raw classification values — `party`, `doc_type`, `detail`,
`doc_date`), `tags`, `provenance` (`rule` or `agent`), `rule_id`, `date_source` (`agent`,
`rule-regex`, `first`, `last`, or `null` for an unclassified entry — where `doc_date` came from,
so a wrong date is diagnosable from the plan alone), and `status` — `move` (rename
and/or folder change), `noop` (target equals current path), `unclassified`, `collision` (two
documents render the same target, or the target already exists and isn't this document's own
path — never auto-suffixed; a human resolves it), or `error` (e.g. a folder that would resolve
outside the root).

> **Security-critical: `target_path` vs. `fields`.** `entries[].target_path` is the only
> sanitized, root-verified value in a plan file, and the only one `apply` (phase 6) may act on.
> `entries[].fields` holds the raw, pre-sanitization classification values for display and
> debugging — `party`/`detail`/`doc_date` come from OCR text (attacker-influenceable content
> inside a scanned document) and can contain path separators, `..`, or other characters that are
> stripped from a filename. `apply` must never re-render a name from `fields`, and must re-run
> the containment check on `target_path` (`resolved.is_relative_to(root.resolve())`) immediately
> before touching the filesystem — `propose`'s own containment check is time-of-check, and
> `apply` runs later, against a filesystem that may have changed underneath it.

Rendering a target name is layered defense, because phase 6 executes these paths: (1) a
`folder` that is absolute, drive-qualified, or contains `..` is rejected at taxonomy-load time
(and identically for an agent-supplied `--folder`); (2) `sanitize_component` NFKC-normalizes
each rendered field, replaces every path separator and anything outside `[A-Za-z0-9._-]` with
`_`, strips leading/trailing separators, and guards Windows-reserved device names
(`CON`, `PRN`, `NUL`, `COM1-9`, `LPT1-9`); (3) the final target is resolved against the root and
must be `is_relative_to` it, or the entry becomes `status='error'` instead of a move. The
template (`[naming].template`, default `{doc_date}_{party}_{doc_type}_{detail}`) is tokenized,
never passed to `str.format`, so a document whose OCR text literally contains `{party}` cannot
influence rendering; an unresolved field drops together with its preceding separator (no
`detail` renders `2026-02-03_Northwind_invoice`, not a trailing underscore).

`propose` is **read-only on both the document tree and the index**: it opens nothing under
`[paths].root` for writing (the plan file is the only output, and it is forced outside the root)
and writes nothing to `document` — `doc_date`/`party`/`doc_type`/`detail` stay `NULL` until
`apply` commits an approved plan. The only index write in this phase is the explicit `classify`
verdict.

`apply <plan>` (phase 6) executes an approved plan against `target_path`, writes `move_log`, and
checks mtime stability and file locks first so it never fights the sync client mid-write.
`undo <plan_id>` reverses from the log.

The date in a filename is the date the document pertains to, extracted from OCR text (ISO,
`D Month YYYY`, `Month D, YYYY`, or numeric — `[taxonomy].date_order`, default `dmy`, breaks a
numeric ambiguity; an invalid calendar date such as `31/02/2026` yields no date rather than a
guess). Scan date stays in the index.

**Per-rule date selection.** A document's front page often carries several dates, and the one a
document is filed by is not always the first one on the page — a bank statement, for example,
usually shows an issue date plus both ends of the statement period. A `[[rules]]` entry can name
which date it means: an optional `date_regex` (case-insensitive, searched, exactly one capture
group) captures the date directly; an optional `date = "first" | "last"` selects among every
parseable date in the text when no regex is given, or as the fallback when the regex is absent or
finds nothing parseable. A rule with neither key keeps the original first-date-in-the-document
behaviour, so adding these keys to one rule never changes what any other rule does. Precedence is
**agent verdict › `date_regex` › `date` selector › first-date default** — an explicit `classify`
verdict always outranks a rule. The winning source is recorded per entry as `date_source` (above),
never silently. **Security-relevant:** a `date_regex` capture is only ever *parsed*, never
interpolated — it is handed to the same calendar parser as every other date source, so `doc_date`
is structurally either `None` or an `_iso`-validated `YYYY-MM-DD`; an operator regex cannot smuggle
path separators or other characters into a proposed name through this route, and `sanitize_component`
still runs on every rendered field regardless. `date_regex` shares `regex`'s trust posture — it is
operator-authored config compiled once at load and run over a length-capped window — so keep it
small, and note that it is deliberately single-line scoped (`.` does not cross a newline): a
period line an OCR pass breaks mid-way falls through to a `date = "last"` companion instead of
matching garbage, which is why the shipped example rule ships both keys together.

**Config-relative path resolution.** A relative value under `[paths]` in `config.toml` (`root`,
`data_dir`, `snapshot_dir`, `taxonomy`, `plan_dir`) resolves against **the config file's own
directory**, not the process's working directory — so a scheduled task or an agent invoked from
an unrelated `cwd` still finds the right instance. `--flags` and `FC_*` environment variables are
shell inputs and keep their working-directory-relative meaning; an absolute `[paths]` value
passes through unchanged either way. `propose`'s text and `--json` output report the taxonomy
path it resolved and its rule count (or `missing, 0 rules`), so an all-unclassified run is never
a silent miss of the wrong file.

**Tools never move or rename files unasked.** This is the invariant every change is measured
against.

## 7. Agent surface (phase 7)

Every CLI verb takes `--json`. A thin MCP server wraps the verbs as typed tools: `ingest`,
`status`, `find`, `dupes`, `propose`, `apply`, `undo`. The engine and CLI need none of the MCP
dependency; `pip install filingcabinet[mcp]` adds it.

## 8. Snapshot and restore (index only)

Independent of the phase chain above — the index needs backup/recovery regardless of which
phases have landed.

`filingcabinet snapshot` writes a timestamped, consistent copy of the live index to
`[paths].snapshot_dir` via `VACUUM INTO` (never a raw file copy — WAL mode makes that unsafe).
Repeated runs rotate: every snapshot from the last 14 days is kept, then the newest per ISO week
for 12 weeks beyond that; the newest snapshot is never deleted.

`filingcabinet restore latest` (or an explicit snapshot path) validates the chosen file first
(`PRAGMA integrity_check`, `schema_migrations` present) and aborts before touching anything if
validation fails. On success it banks a rescue copy of the index it's about to replace under
`[paths].data_dir/rescue/` (so a restore is itself undoable), clears stale `-wal`/`-shm`
sidecars left by the replaced index, runs forward migrations so an older snapshot lands on the
current schema, and reports row counts per table. Neither verb reads or writes anything under
`[paths].root` — both operate on the index only.

`[paths].snapshot_dir` is trusted storage, not just a backup location: `restore latest` installs
whatever file parses as the newest snapshot there, so keep it on a synced folder only you write
to.

## 9. Instance model and privacy

- **Framework repo (this one):** public-capable. `.gitignore` blocks document formats and
  databases; `npm run check:docs` fails CI on any tracked offender.
- **Instance repo (per user, private):** `config.toml`, taxonomy and rules, index snapshots,
  the move-log export. Documents stay in the Drive folder; Drive is their backup. This
  deliberately diverges from pemr-data, which commits a content-addressed copy of every scan.
  Scaffold one with `filingcabinet instance init <dir>` - see
  [Development_Instance.md](Development_Instance.md).
- **pemr:** independent. Both projects use sha256 content IDs, so a later bridge (pemr
  referencing a FilingCabinet document) is a lookup, not a dependency. No shared code or DB.

Security-relevant rule the OCR path (§5) upholds and any later rung (`drive`, or a toolchain
installer) must preserve: document content never reaches a shell (tesseract runs as an argv list,
never `shell=True`, on a code-constructed path, with a timeout) and OCR never writes under
`[paths].root` (rasterization lands in an OS temp dir) — the same read-only-on-the-tree posture
as ingest and dedup.

## 10. Open questions

- Local-to-Drive drift that has not synced down as a file needs the Drive API. Deferred.
- Windows OCR toolchain (tesseract) install remains manual (`doctor` reports presence/version,
  landed in phase 4; an installer or `doctor --fix` is still open). See
  [Development.md](Development.md).
- graphify over OCR text exports for cross-document entity linking. Experiment after phase 4.
