# The instance repo

FilingCabinet is split in two (see [Architecture.md](Architecture.md) §8):

| Framework repo (this one) | Instance repo/directory (yours, private) |
|---|---|
| engine, CLI, migrations, tests, docs | `config.toml` for your machine |
| public-capable | taxonomy and naming rules |
| **never** holds user data | index snapshots (`snapshot_dir` exports) |
| `.gitignore` + `npm run check:docs` enforce that | the move-log export |

Documents themselves live in the Drive-synced folder and are never copied into either repo —
Drive is their backup. This deliberately diverges from `pemr-data`, which commits a
content-addressed copy of every scan.

The live index database is in neither: it sits at `[paths].data_dir`, which must be a local-only
directory. Cloud sync corrupts SQLite WAL sidecars mid-write; only the `VACUUM INTO` snapshots in
`snapshot_dir` are safe to sync.

## Standing one up

```
filingcabinet instance init C:\filingcabinet-instance
```

Sample output:

```
initialized instance at C:\filingcabinet-instance: 4 created, 0 skipped
```

The verb scaffolds four files and nothing else — no `git init`, no remote, no network call.
Whether the directory becomes a git repo is your call.

| File | Purpose |
|---|---|
| `config.toml` | seeded from the framework's `config.example.toml`; edit for your machine |
| `README.md` | the "do not grant broad Drive or app access" warning, what lives here, and the ingest-scheduling recipe |
| `.gitignore` | backstop blocking document formats, databases, and credential files |
| `CLAUDE.md` | pins the framework version this instance tracks and restates the invariants for agents |

`instance init` is idempotent and **never overwrites**: re-running it reports existing files as
`skipped` and leaves your edits alone. `--force` rewrites them (it will clobber an edited
`config.toml` — take a copy first). `--json` emits `{"path", "created", "skipped"}`.

## Editing `config.toml`

At minimum set `[paths]`:

- `root` — the Drive-synced document folder. Bytes live here.
- `inbox` — where the scanner or phone drops new files (usually under `root`).
- `data_dir` — **local-only**, never inside Drive/OneDrive/Dropbox. The live index runs in WAL
  mode and cloud sync will corrupt it.
- `snapshot_dir` — cloud-synced; consistent single-file snapshots land here.

Then create the index:

```
filingcabinet --config C:\filingcabinet-instance\config.toml migrate --create
filingcabinet --config C:\filingcabinet-instance\config.toml status
```

## Scheduling the ingest sweep

The recurring sweep runs **from the instance, not from the framework checkout**. The framework
checkout is neither the working directory for a scheduled run nor a place user data may land;
point every invocation at the instance's `config.toml` with `--config`.

Windows Task Scheduler (the primary host):

```
schtasks /Create /TN FilingCabinetIngest /SC HOURLY ^
  /TR "python -m filingcabinet --config C:\filingcabinet-instance\config.toml ingest"
```

cron:

```
0 * * * * python -m filingcabinet --config /home/you/filingcabinet-instance/config.toml ingest
```

Use `python -m filingcabinet` rather than the `fc` console script where `pip install -e .` could
not create the shim (Windows). Add `--json` if the schedule feeds a log parser.

`ingest` only reads the document root and writes index rows — it never moves or renames a
document. Reorganization stays behind `propose` / `apply`
([Architecture.md](Architecture.md) §6).

## Writing taxonomy rules

`propose` (`docs/Architecture.md` §6) classifies against `taxonomy.toml`, which lives beside
`config.toml` in your instance directory by default (`[paths].taxonomy`, or `--taxonomy` /
`FC_TAXONOMY` to point elsewhere) — never in the framework repo. `instance init` scaffolds a
starting file; `filingcabinet/templates/taxonomy.example.toml` in the framework repo documents
the format with synthetic vendors.

- **Matching order.** Rules run in `(priority descending, id ascending)` order; the first match
  wins. A document no rule matches is `unclassified` in the plan and waits for an agent verdict.
- **Recording a verdict:** `filingcabinet classify --document <id> --party P --doc-type T
  [--detail D] [--doc-date YYYY-MM-DD] [--folder F] [--tag T ...]`. This persists one row per
  document (`classification` table, `provenance='agent'`) and **outranks** a competing rule on
  the next `propose`.
- **Promoting a verdict into a rule.** `classify` prints a paste-ready `[[rules]]` stanza in its
  output. Paste it into `taxonomy.toml` yourself and adjust `all`/`any`/`none`/`regex` as needed
  — `classify` never edits the rules file; that is your call, the same posture as `apply` never
  moving a file unasked.
- **A missing or empty `taxonomy.toml` is not an error** — every document just routes to the
  agent. `propose`'s text and `--json` output report the exact file path it read and its rule
  count, so an all-unclassified run because the wrong file was read is never silent.
- Rule text is matched case-insensitively against whitespace-normalized OCR text; a party's
  `aliases` widen the `any` terms of every rule naming that party. Keep `regex` patterns small —
  matching is capped at the first 200k characters of a document's text.

## Privacy rules

- Never grant an agent, script, or third-party app broad Drive or account access; scope every
  credential to the single indexed document folder.
- Never commit Drive credentials, OAuth tokens, or service-account keys — the instance
  `.gitignore` blocks the common filenames, but that is a backstop, not permission.
- No document bytes in either repo. `npm run check:docs` fails CI here on any tracked offender.

See also [Development.md](Development.md) for local setup and test commands.
