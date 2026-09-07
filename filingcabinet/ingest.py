"""Phase 2 scan + hash index (docs/Architecture.md §3).

Walks [paths].root, sha256s new or changed files, records document + occurrence rows and
marks vanished paths missing_since. Incremental: a file whose (mtime, size) match the
recorded occurrence is not re-hashed. Read-only on the document tree - nothing here
renames, moves, or writes a file under the root (docs/Architecture.md §6); writes are
confined to the SQLite index.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import db

DEFAULT_EXTENSIONS = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".webp"}
)

# Sync-client scratch, Office lock files, and partial downloads: never documents.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".*",
    "~$*",
    "*.tmp",
    "*.crdownload",
    "*.driveupload",
    "*.tmp.drivedownload",
)

_NUMBERED_COPY = re.compile(r"\s\(\d+\)$")

try:  # PyMuPDF is an optional extra ([ocr]/[dedup]); page counts degrade to None.
    import fitz as _fitz
except Exception:  # pragma: no cover - depends on the local environment
    _fitz = None


@dataclass(frozen=True)
class IngestSummary:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    """UTC ISO timestamp, microsecond precision.

    Finer than ``db.py``'s second resolution, but never used for ordering: the host
    clock ticks coarsely enough that two runs can share a timestamp, so the missing
    sweep orders by :func:`start_scan`'s monotonic ``scan_id`` instead.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def start_scan(conn: sqlite3.Connection, *, now: str) -> int:
    """Open a scan row and return its ``scan_id`` - this run's monotonic token.

    AUTOINCREMENT guarantees a strictly greater id than every previous run's, which is
    what makes the missing sweep independent of wall-clock resolution.
    """
    cursor = conn.execute("INSERT INTO scan (started_at) VALUES (?)", (now,))
    return int(cursor.lastrowid)


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    lowered = name.lower()
    return any(fnmatch(lowered, pattern.lower()) for pattern in patterns)


def iter_candidates(
    root: Path,
    extensions: frozenset[str] | None = None,
    excludes: tuple[str, ...] | None = None,
) -> Iterator[Path]:
    """Yield candidate document files under ``root``.

    Prunes excluded directories in place and never follows symlinks, so a link loop
    cannot hang a scheduled run.
    """
    extensions = DEFAULT_EXTENSIONS if extensions is None else extensions
    excludes = DEFAULT_EXCLUDES if excludes is None else excludes
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _matches_any(d, excludes)]
        for filename in filenames:
            if _matches_any(filename, excludes):
                continue
            if Path(filename).suffix.lower() not in extensions:
                continue
            yield Path(dirpath) / filename


def rel_path_of(root: Path, path: Path) -> str:
    """Path relative to ``root`` in the schema's stored form (forward slashes)."""
    return path.relative_to(root).as_posix()


def classify_conflict(name: str) -> str | None:
    """Flag Drive conflict-copy filename patterns; None for ordinary names."""
    if "conflicted copy" in name.casefold():
        return "drive_conflicted_copy"
    if _NUMBERED_COPY.search(Path(name).stem):
        return "drive_numbered"
    return None


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Streaming sha256 - never reads a whole document into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path) -> tuple[str | None, int | None]:
    """(mime, page_count). Page count only for PDFs, and only when PyMuPDF is installed."""
    mime, _ = mimetypes.guess_type(path.name)
    page_count = None
    if _fitz is not None and path.suffix.lower() == ".pdf":
        try:
            with _fitz.open(path) as doc:
                page_count = doc.page_count
        except Exception:
            page_count = None
    return mime, page_count


def _upsert_document(
    conn: sqlite3.Connection, sha256: str, size: int, mime: str | None, pages: int | None, now: str
) -> int:
    conn.execute(
        """
        INSERT INTO document (sha256, size_bytes, mime, page_count, first_seen_at, updated_at)
        VALUES (:sha, :size, :mime, :pages, :now, :now)
        ON CONFLICT(sha256) DO UPDATE SET
          mime       = COALESCE(document.mime, excluded.mime),
          page_count = COALESCE(document.page_count, excluded.page_count),
          updated_at = excluded.updated_at
        """,
        {"sha": sha256, "size": size, "mime": mime, "pages": pages, "now": now},
    )
    return conn.execute(
        "SELECT document_id FROM document WHERE sha256 = ?", (sha256,)
    ).fetchone()["document_id"]


def upsert_file(
    conn: sqlite3.Connection, root: Path, path: Path, *, now: str, scan_id: int
) -> str:
    """Index one file. Returns ``"new" | "changed" | "unchanged"``.

    Skips hashing when the recorded (mtime, size) still match and the path is not marked
    missing - the load-bearing incremental optimization.
    """
    rel = rel_path_of(root, path)
    stat = path.stat()
    conflict = classify_conflict(path.name)
    row = conn.execute(
        "SELECT occurrence_id, mtime, size_bytes, missing_since, conflict_kind "
        "FROM occurrence WHERE rel_path = ?",
        (rel,),
    ).fetchone()
    if (
        row is not None
        and row["missing_since"] is None
        and row["mtime"] == stat.st_mtime
        and row["size_bytes"] == stat.st_size
    ):
        conn.execute(
            "UPDATE occurrence SET seen_at = ?, conflict_kind = ?, last_scan_id = ? "
            "WHERE occurrence_id = ?",
            (now, conflict, scan_id, row["occurrence_id"]),
        )
        return "unchanged"

    sha256 = sha256_file(path)
    mime, pages = probe(path)
    document_id = _upsert_document(conn, sha256, stat.st_size, mime, pages, now)
    conn.execute(
        """
        INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at,
                                missing_since, conflict_kind, hashed_at, last_scan_id)
        VALUES (:doc, :rel, :mtime, :size, :now, NULL, :conflict, :now, :scan_id)
        ON CONFLICT(rel_path) DO UPDATE SET
          document_id   = excluded.document_id,
          mtime         = excluded.mtime,
          size_bytes    = excluded.size_bytes,
          seen_at       = excluded.seen_at,
          missing_since = NULL,
          conflict_kind = excluded.conflict_kind,
          hashed_at     = excluded.hashed_at,
          last_scan_id  = excluded.last_scan_id
        """,
        {
            "doc": document_id,
            "rel": rel,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "now": now,
            "conflict": conflict,
            "scan_id": scan_id,
        },
    )
    return "changed" if row is not None else "new"


def mark_missing(conn: sqlite3.Connection, *, scan_id: int, now: str) -> int:
    """Mark paths this scan did not see as missing (never delete). Returns the count.

    Ordering is by ``last_scan_id``, not by timestamp: a row this run touched carries
    exactly ``scan_id``, so anything else is unseen regardless of clock resolution.
    Rows written before migration 003 have ``last_scan_id IS NULL`` and are swept too.
    """
    cursor = conn.execute(
        "UPDATE occurrence SET missing_since = ? "
        "WHERE missing_since IS NULL AND (last_scan_id IS NULL OR last_scan_id < ?)",
        (now, scan_id),
    )
    return cursor.rowcount


def run_ingest(
    conn: sqlite3.Connection,
    root: Path,
    *,
    extensions: frozenset[str] | None = None,
    excludes: tuple[str, ...] | None = None,
) -> IngestSummary:
    """Scan ``root``, index new/changed files, reconcile missing paths."""
    db.require_migrated(conn)
    root = Path(root).resolve()
    scan_id = start_scan(conn, now=_now())
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    scanned = 0
    errors = 0

    for path in iter_candidates(root, extensions, excludes):
        scanned += 1
        try:
            resolved = path.resolve()
            resolved.relative_to(root)  # symlink escape: never index under a fake rel_path
        except (OSError, ValueError):
            errors += 1
            continue
        try:
            with conn:  # commit per file: an interrupted run is resumable
                counts[upsert_file(conn, root, path, now=_now(), scan_id=scan_id)] += 1
        except OSError:
            errors += 1  # locked mid-Drive-sync: count and carry on

    with conn:
        missing = mark_missing(conn, scan_id=scan_id, now=_now())

    return IngestSummary(
        scanned=scanned,
        new=counts["new"],
        changed=counts["changed"],
        unchanged=counts["unchanged"],
        missing=missing,
        errors=errors,
    )
