# Overview

**FilingCabinet** is a local-first framework of programmatic tools an AI assistant uses to OCR,
deduplicate, and organize a personal collection of scanned documents. The documents themselves
live in a Google Drive-synced folder on the user's machine; FilingCabinet keeps a SQLite index
beside them that is the source of truth for **identity and metadata** (content hash, OCR text,
classification, proposed names, applied moves) and never for the bytes.

It is a sibling of [pemr](https://github.com/meridun/pemr) in shape: a Python CLI engine, a
SQLite database, a thin MCP wrapper, and a strict "no user data in this repo" posture. This
repository is the framework and its documentation. A user's own collection is a private
**instance**: a small git repo holding config, taxonomy rules, index snapshots, and the
move-log, with the documents staying in Drive.

## Who it is for

One person (or household) with an ongoing inflow of scans who wants better search and a
consistent, descriptive filing scheme than a cloud drive's folder tree and built-in OCR give
them, and who works with an AI assistant that should call typed tools instead of re-deriving
the logic on every request.

## Main subsystems

| Subsystem | Doc | Phase |
|---|---|---|
| Index and identity (sha256 document IDs, occurrences, migrations) | [Architecture.md](Architecture.md) | 1-2 |
| Dedup (exact, near-duplicate rescans, subset pages) | Architecture.md § Dedup | 3 |
| OCR ladder and full-text search | Architecture.md § OCR | 4 |
| Rules, classification, and naming proposals | Architecture.md § Organize | 5 |
| Apply plans, move-log, undo | Architecture.md § Organize | 6 |
| MCP wrapper (agent surface) | Architecture.md § Agent surface | 7 |
| Snapshot + restore of the index | Architecture.md § Snapshot and restore | - |
| graphify experiment (cross-document entity linking) | [Development_GraphifyExperiment.md](Development_GraphifyExperiment.md) | 8 |

Local setup, commands, and the branch model: [Development.md](Development.md). Shared repo
config adopted from model-repo: the `## Shared config` table in the [README](../README.md).
