#!/usr/bin/env python
"""Throwaway export of per-document OCR text for the phase-8 graphify spike (issue #8).

Dumps one Markdown file per indexed document — front matter (identity + filing metadata)
plus the OCR text — so a knowledge-graph tool can be run over the corpus. Read-only over
the index: SELECT only, no move-log row, nothing renamed or deleted
(docs/Architecture.md §6).

Deliberately **not** wired into `filingcabinet/cli.py`: productizing an `export` verb is
gated on the spike's go/no-go verdict (docs/Development_GraphifyExperiment.md), so this
stays a script.

The output is real document content. `refuse_inside_repo` makes an in-repo `--out`
impossible by construction, because docs/Architecture.md §8 ("no user documents in this
repo") must not depend on operator care. Filenames are content-free: a scan's own filename
is personal data, so it never appears in an exported name.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from filingcabinet import db

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "manifest.json"

# Same live-occurrence subquery shape as search._FIND_SQL: first non-missing path, by id.
_EXPORT_SQL = """
    SELECT d.document_id AS document_id,
           d.sha256      AS sha256,
           d.mime        AS mime,
           d.page_count  AS page_count,
           d.doc_date    AS doc_date,
           d.party       AS party,
           d.doc_type    AS doc_type,
           d.ocr_text    AS ocr_text,
           (SELECT o.rel_path FROM occurrence o
             WHERE o.document_id = d.document_id AND o.missing_since IS NULL
             ORDER BY o.occurrence_id LIMIT 1) AS rel_path
    FROM document d
    WHERE d.ocr_text IS NOT NULL AND TRIM(d.ocr_text) <> ''
    ORDER BY d.document_id
"""

_PAGES_SQL = """
    SELECT page_number, text FROM page_ocr
    WHERE document_id = ? AND status = 'ok' AND text IS NOT NULL AND TRIM(text) <> ''
    ORDER BY page_number
"""

_FRONT_MATTER_FIELDS = (
    "document_id",
    "sha256",
    "mime",
    "page_count",
    "doc_date",
    "party",
    "doc_type",
)


@dataclass(frozen=True)
class ExportedDoc:
    document_id: int
    sha256: str
    rel_path: str | None
    page_count: int | None
    chars: int
    out_file: str

    def as_dict(self) -> dict:
        return asdict(self)


def refuse_inside_repo(target: Path) -> Path:
    """Resolve ``target`` and refuse any path inside this framework repo.

    Exported OCR text is user document content; docs/Architecture.md §8 keeps it out of
    the repo entirely. There is no override flag on purpose.
    """
    resolved = Path(target).expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        print(
            f"error: refusing to write document content inside the framework repo ({resolved}) "
            "- choose a path outside it (docs/Architecture.md §8)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return resolved


def iter_documents(conn: sqlite3.Connection, *, limit: int | None = None) -> Iterator[sqlite3.Row]:
    """Documents holding non-empty OCR text, lowest document_id first.

    Ordering is deterministic so ``limit`` (the spike's token-cost cap) samples the same
    documents on every run.
    """
    db.require_migrated(conn)
    remaining = None if limit is None else max(0, int(limit))
    for row in conn.execute(_EXPORT_SQL):
        # SQL TRIM() only strips spaces, so a newline-only ocr_text survives the WHERE clause.
        if not str(row["ocr_text"] or "").strip():
            continue
        if remaining is not None:
            if remaining == 0:
                return
            remaining -= 1
        yield row


def page_texts(conn: sqlite3.Connection, document_id: int) -> list[tuple[int, str]]:
    """Per-page OCR text for ``document_id``; empty when the document predates page rows."""
    return [(int(row["page_number"]), row["text"]) for row in conn.execute(_PAGES_SQL, (document_id,))]


def out_name(row: sqlite3.Row) -> str:
    """Content-free filename: document id plus a sha prefix, never the source filename."""
    return f"{int(row['document_id']):05d}-{str(row['sha256'])[:12]}.md"


def render(row: sqlite3.Row, pages: list[tuple[int, str]]) -> str:
    """One Markdown document: front matter, then per-page sections (or the rolled-up text)."""
    lines = ["---"]
    for field in _FRONT_MATTER_FIELDS:
        value = row[field]
        lines.append(f"{field}: {'' if value is None else value}")
    lines.append(f"source_rel_path: {row['rel_path'] or ''}")
    lines.append("---")
    lines.append("")
    if pages:
        for number, text in pages:
            lines.append(f"## Page {number}")
            lines.append("")
            lines.append(text.strip())
            lines.append("")
    else:
        lines.append("## Text")
        lines.append("")
        lines.append(str(row["ocr_text"]).strip())
        lines.append("")
    return "\n".join(lines)


def export(conn: sqlite3.Connection, out_dir: Path, *, limit: int | None = None) -> list[ExportedDoc]:
    """Write one Markdown file per document under ``out_dir``, plus a text-free manifest."""
    out_dir = refuse_inside_repo(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    exported: list[ExportedDoc] = []
    for row in iter_documents(conn, limit=limit):
        pages = page_texts(conn, int(row["document_id"]))
        body = render(row, pages)
        name = out_name(row)
        (out_dir / name).write_text(body, encoding="utf-8")
        exported.append(
            ExportedDoc(
                document_id=int(row["document_id"]),
                sha256=str(row["sha256"]),
                rel_path=row["rel_path"],
                page_count=row["page_count"],
                chars=len(body),
                out_file=name,
            )
        )

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "document_count": len(exported),
        "documents": [
            {
                "document_id": doc.document_id,
                "sha256": doc.sha256,
                "out_file": doc.out_file,
                "chars": doc.chars,
            }
            for doc in exported
        ],
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return exported


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_ocr_text",
        description="Export per-document OCR text as Markdown (phase-8 graphify spike, issue #8).",
    )
    parser.add_argument("--db", required=True, help="index database path")
    parser.add_argument("--out", required=True, help="output directory (must be outside this repo)")
    parser.add_argument("--limit", type=int, help="export at most N documents (lowest ids first)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if not db.database_exists(db_path):
        raise SystemExit(f"error: no database at {db_path} - run `filingcabinet migrate --create`")

    out_dir = refuse_inside_repo(Path(args.out))
    conn = db.connect(db_path)
    try:
        exported = export(conn, out_dir, limit=args.limit)
    finally:
        conn.close()

    if args.json:
        print(
            json.dumps(
                {
                    "db": str(db_path),
                    "out": str(out_dir),
                    "document_count": len(exported),
                    "documents": [doc.as_dict() for doc in exported],
                },
                indent=2,
            )
        )
    else:
        print(f"exported {len(exported)} documents to {out_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
