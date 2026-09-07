# FilingCabinet

Local-first tools an AI assistant uses to **OCR, deduplicate, and organize scanned documents**.
The documents stay in a Google Drive-synced folder on your machine; a **SQLite index is the
source of truth for identity and metadata** (sha256 document IDs, OCR text, classification,
proposed names, applied moves), never for the bytes. Deterministic work lives in a **Python CLI
engine** wrapped by a **thin MCP server**, so agents call typed tools instead of re-inventing
the logic every request.

> ⚠️ **This repository is framework + documentation only. No user documents live here.** The
> index, document root, inbox, snapshots, and plans all reside outside the repo. `.gitignore`
> blocks document formats and databases; `npm run check:docs` fails CI on any tracked offender.
> A user's own collection is a separate private *instance* repo (config, taxonomy, snapshots,
> move-log) and the documents themselves stay in Drive.

## What it does

- **Index without moving anything** — walk the folder, hash every file, track where each
  document lives. Tools never rename or move a file unasked.
- **Dedup in three tiers** — exact hash, near-duplicate rescans (perceptual page hashes + OCR
  similarity), and subset detection (pages of one scan inside another). Uncertain matches go
  to a review queue.
- **OCR ladder** — local tesseract first, Drive's text layer when the API lands, agent vision
  as a last resort. Full-text search over the result.
- **Propose, then apply** — rules classify known vendors, the agent handles the rest, and
  `propose` writes a plan a human reviews. `apply` executes it with a move-log and `undo`.

Full design in [docs/Architecture.md](docs/Architecture.md); setup in
[docs/Development.md](docs/Development.md).

## Design decisions

| Area | Choice |
|---|---|
| Bytes | Drive-synced local folder; Drive client replicates. No Drive API in v1. |
| Identity / metadata | SQLite on a non-synced local path; `VACUUM INTO` snapshots sync. |
| Document ID | sha256 of file bytes; filenames are derived labels. |
| Organize | Index + propose; human confirms; `apply` explicit, logged, reversible. |
| Dedup | Exact + near-duplicate + subset, review queue for the uncertain. |
| OCR | local → drive → vision, per-page confidence gate. |
| Classification | Rules first, agent for the remainder; agent verdicts promotable to rules. |
| Interface | Python CLI (`filingcabinet` / `fc`, every verb `--json`) + thin MCP wrapper. |
| pemr | Independent. Shared sha256 IDs make a later bridge a lookup, not a dependency. |

## Status

Design locked 2026-09-07. Phases: **1 skeleton** (this) → 2 scan + hash index → 3 dedup →
4 OCR + FTS → 5 rules + propose → 6 apply + undo → 7 MCP → 8 graphify experiment. Each phase
is a GitHub issue.

## Shared config

Adoption record for [meridun/model-repo](https://github.com/meridun/model-repo) components (see
`.github/skills/fc-upstream-sync/SKILL.md`). Created from the template at model-repo **72ceda9**
(2026-09-07). Declined rows are deliberate and revisitable.

| Component | Status | Pin | Notes |
|---|---|---|---|
| Doc-tier system (L1/L2/L3) | adopted | 72ceda9 | prefix `fc-`; L1 trimmed per pemr: no `## Tone`, graphify and Token wrappers are pointers |
| Config sync + meta-drift guard | adopted | 72ceda9 | `fc-wt` worktree prefix allowlisted in `check-meta-drift.mjs` |
| Caveman mode hook | adopted | 72ceda9 | L1 canonical; hook drift-checked |
| graphify nudge hook + vtk notes | adopted | 72ceda9 | vtk in transparent-wrapper mode |
| Role-based model routing | adopted | 72ceda9 | pin in `docs/Development_ModelRouting.md` |
| Agentic SDLC pipeline | partial | 72ceda9 | core + `gh-issue` binding + CLI adopted; `sdlc/tools/` lint ratchet **declined** (ESLint-only, Python repo); ADO bindings declined; `PROD_BRANCH = master` |
| Upstream sync procedure | adopted | 72ceda9 | `fc-upstream-sync` |
| pemr `check:pii` roster guard | declined | — | replaced by `check:docs` (document/database file guard); no synthetic-identity roster needed here |
