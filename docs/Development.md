# Development

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[ocr,dedup]" pytest
npm install                   # SDLC and config-sync tooling only
cp config.example.toml config.toml   # then edit paths for this machine
filingcabinet migrate --create
filingcabinet status
```

`fc` is an alias for `filingcabinet`. Every verb accepts `--json`, `--db`, and `--config`;
`FC_DB` and `FC_CONFIG` environment variables override `config.toml`.

## System dependencies (phase 4 onward)

Local OCR needs `tesseract` on `PATH`; PDF rasterization goes through PyMuPDF (no Ghostscript
needed unless ocrmypdf is adopted). On Windows:

```bash
winget install UB-Mannheim.TesseractOCR
```

Without it, OCR degrades to embedded text layers only: a rasterized page is left `pending_vision`
with `note='tesseract_missing'` and counted in `ocr run --json`'s `degraded` total, never an
error. `filingcabinet doctor` reports whether tesseract is on `PATH`. Installing tesseract and
re-running `ocr run` picks those pages back up automatically.

## Tests

```bash
pytest                        # full Python suite
pytest tests/test_cli.py      # targeted
npm test                      # SDLC / meta tooling (node --test)
npm run check:docs            # no user documents tracked
npm run check:meta-drift      # L1/L2 drift guard
npm run sync:claude-config:check
```

## Branch model

`{feature} → dev → master`. `dev` is the default and integration branch; `master` is prod and
never receives direct pushes. The SDLC pipeline (`docs/Development_AgenticSDLC.md`,
`sdlc/PROFILE.md`) runs on GitHub issues with the `stage:*` label taxonomy in
`sdlc/bindings/gh-issue/labels.md`.

## Data locations

Never put the live index inside a cloud-synced folder. `config.example.toml` explains each
path. Nothing under `data/`, `inbox/`, `snapshots/`, `plans/`, or any `*.pdf`/`*.db` is
committable; `.gitignore` and `check:docs` both enforce it.
