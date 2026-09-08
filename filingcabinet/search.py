"""Phase 4 full-text search over OCR text (docs/Architecture.md §5).

Queries the `document_fts` FTS5 index built by migration 005 and shapes the hits for the
`find` verb. Read-only: nothing here writes the index or the document tree.

User query text is untrusted: `escape_query` wraps a bare phrase as an FTS5 string literal so
an apostrophe or a hyphen cannot become a syntax error, and a genuinely malformed expression
surfaces as ValueError for the CLI to turn into an error message, never a traceback.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass

from . import db

DEFAULT_LIMIT = 20

# Documented FTS5 syntax the caller may legitimately be using; anything else is a bare phrase.
_OPERATORS = re.compile(r'["*:]|(?:^|\s)(?:AND|OR|NOT|NEAR)(?:\s|$)')

_FIND_SQL = """
    SELECT d.document_id AS document_id,
           d.sha256      AS sha256,
           d.mime        AS mime,
           d.page_count  AS page_count,
           (SELECT o.rel_path FROM occurrence o
             WHERE o.document_id = d.document_id AND o.missing_since IS NULL
             ORDER BY o.occurrence_id LIMIT 1) AS rel_path,
           snippet(document_fts, 0, '[', ']', ' ... ', 12) AS snippet,
           document_fts.rank AS rank
    FROM document_fts
    JOIN document d ON d.document_id = document_fts.rowid
    WHERE document_fts MATCH ?
    ORDER BY document_fts.rank
    LIMIT ?
"""


@dataclass(frozen=True)
class Hit:
    document_id: int
    sha256: str
    rel_path: str | None
    mime: str | None
    page_count: int | None
    snippet: str
    rank: float

    def as_dict(self) -> dict:
        return asdict(self)


def escape_query(query: str) -> str:
    """Make ``query`` safe to hand to FTS5 MATCH, preserving deliberate operator syntax."""
    stripped = (query or "").strip()
    if not stripped:
        raise ValueError("empty search query")
    if _OPERATORS.search(stripped):
        return stripped
    return '"' + stripped.replace('"', '""') + '"'


def find(conn: sqlite3.Connection, query: str, *, limit: int = DEFAULT_LIMIT) -> list[Hit]:
    """Documents whose OCR text matches ``query``, best first."""
    db.require_migrated(conn)
    match = escape_query(query)
    limit = max(1, int(limit))
    try:
        rows = list(conn.execute(_FIND_SQL, (match, limit)))
    except sqlite3.OperationalError as exc:
        raise ValueError(f"invalid search query: {exc}") from exc
    return [
        Hit(
            document_id=row["document_id"],
            sha256=row["sha256"],
            rel_path=row["rel_path"],
            mime=row["mime"],
            page_count=row["page_count"],
            snippet=row["snippet"] or "",
            rank=float(row["rank"]),
        )
        for row in rows
    ]
