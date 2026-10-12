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
- **OCR ladder** — local tesseract first, then Drive's text layer (opt-in: fetched unattended
  through the Drive API once `ocr auth` has stored a token in `data_dir`, or submitted by an
  agent with `ocr submit --source drive`; document-level), agent vision as a last resort.
  Full-text search over the result.
- **Propose, then apply** — rules classify known vendors, the agent handles the rest, and
  `propose` writes a plan a human reviews. `apply` executes it with a move-log and `undo`.

Full design in [docs/Architecture.md](docs/Architecture.md); setup in
[docs/Development.md](docs/Development.md).

## Design decisions

| Area | Choice |
|---|---|
| Bytes | Drive-synced local folder; Drive client replicates. Drive API only for the opt-in OCR rung. |
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
(2026-09-07); last synced against **21c127c** (2026-09-30) on 2026-10-12. Declined rows are
deliberate and revisitable.

| Component | Status | Pin | Notes |
|---|---|---|---|
| Doc-tier system (L1/L2/L3) | adopted | 21c127c | prefix `fc-`; L1 trimmed per pemr: no `## Tone`, graphify and Token wrappers are pointers; doc-issue-refs guard adopted 2026-10-12 (`superseded` label created) |
| Config sync + meta-drift guard | adopted | 21c127c | `fc-wt` worktree prefix allowlisted in `check-meta-drift.mjs` |
| Caveman mode hook | adopted | 21c127c | L1 canonical; hook drift-checked |
| graphify nudge hook + vtk notes | adopted | 21c127c | vtk in transparent-wrapper mode |
| Role-based model routing | adopted | 21c127c | pin in `docs/Development_ModelRouting.md` |
| Agentic SDLC pipeline | partial | 21c127c | core + `gh-issue` binding + CLI adopted, including focused dispatch (`focus.md`), bounce summary, prior-work check, and the `node-modules` refresh (no-op here); `sdlc/tools/` lint ratchet **declined** 2026-10-12 again (no Python lint backlog tracked; `LINT_CMD` unbound); ADO bindings **declined** 2026-10-12 again (no Azure DevOps downstream); `PROD_BRANCH = master` |
| Upstream sync procedure | adopted | 21c127c | `fc-upstream-sync` |
| pemr `check:pii` roster guard | declined | — | replaced by `check:docs` (document/database file guard); no synthetic-identity roster needed here |
