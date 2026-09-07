"""Phase 3 dedup (docs/Architecture.md §4): exact, near-duplicate, and subset tiers.

Read-and-report only. Page hashes are computed by rendering pages read-only; every write
lands in the SQLite index. Nothing here renames, moves, merges, or deletes a file under
the document root (docs/Architecture.md §6) - uncertain matches go to the `dupe_review`
queue with status 'pending' and wait for a human verdict.

Tiers, in order of confidence:

1. exact  - several live occurrences of one document_id. Identity is sha256 by schema
            (document.sha256 is UNIQUE), so this tier is zero-false-positive by
            construction, not by heuristic. Reported directly, never queued.
2. near   - equal page counts and every page of A pairs to a distinct page of B within
            [dedup].phash_max_distance. Queued.
3. subset - A has fewer pages than B and every page of A pairs to a distinct page of B
            within the same threshold. Queued.

The image stack (PyMuPDF + ImageHash + Pillow, the `dedup` extra) is optional. When it is
missing, run_report degrades exactly as ingest.probe degrades page_count: tiers 2 and 3
report zero, the summary says phash_available false, and the exit code stays 0.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db

# ImageHash `phash`: 8x8 DCT low-frequency block, 64 bits, 16 hex characters.
PHASH_ALGO = "phash8"
RENDER_DPI = 150
DEFAULT_PHASH_MAX_DISTANCE = 6

# Candidate-pair budget for the pairwise passes. Beyond it a pass stops early and the
# summary's `truncated` flag says so, rather than hanging on a quadratic corpus.
MAX_PAIRS = 20_000

VALID_KINDS = ("near", "subset", "exact")
VALID_VERDICTS = ("dup", "not_dup")

try:  # PyMuPDF is an optional extra ([ocr]/[dedup]); rendering degrades to unavailable.
    import fitz as _fitz
except Exception:  # pragma: no cover - depends on the local environment
    _fitz = None

try:  # ImageHash + Pillow are the [dedup] extra; absent means tiers 2 and 3 report zero.
    import imagehash as _imagehash
    from PIL import Image as _Image
except Exception:  # pragma: no cover - depends on the local environment
    _imagehash = None
    _Image = None


@dataclass(frozen=True)
class DupeSummary:
    exact_groups: int = 0
    exact_documents: int = 0
    near: int = 0
    subset: int = 0
    queued: int = 0
    hashed_documents: int = 0
    hashed_pages: int = 0
    phash_available: bool = False
    truncated: bool = False
    errors: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Match:
    kind: str
    document_a: int
    document_b: int
    score: float
    detail: str


def _now() -> str:
    """UTC ISO timestamp, microsecond precision (matches ingest._now)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def phash_available() -> bool:
    """True when the optional image stack needed for page hashing is importable."""
    return _fitz is not None and _imagehash is not None and _Image is not None


def hamming_hex(a: str, b: str) -> int:
    """Hamming distance between two equal-width hex digests."""
    if len(a) != len(b):
        raise ValueError(f"hash width mismatch: {len(a)} != {len(b)}")
    return (int(a, 16) ^ int(b, 16)).bit_count()


def page_phashes(path: Path, *, dpi: int = RENDER_DPI) -> list[str]:
    """Perceptual hash of every page of `path`, in page order.

    Read-only on the file. A page that fails to render is skipped rather than raising, so
    one bad page does not lose the whole document; the caller sees a short list.
    """
    if not phash_available():
        raise RuntimeError(
            "page hashing needs the optional `dedup` extra (PyMuPDF, ImageHash, Pillow)"
        )
    zoom = dpi / 72.0
    matrix = _fitz.Matrix(zoom, zoom)
    hashes: list[str] = []
    with _fitz.open(path) as doc:
        for page in doc:
            try:
                pixmap = page.get_pixmap(matrix=matrix, colorspace=_fitz.csGRAY)
                mode = {1: "L", 3: "RGB", 4: "RGBA"}.get(pixmap.n)
                if mode is None:  # pragma: no cover - csGRAY always yields n == 1
                    continue
                image = _Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
                hashes.append(str(_imagehash.phash(image)))
            except Exception:
                continue
    return hashes


def pending_hash_documents(
    conn: sqlite3.Connection, *, limit: int | None = None
) -> list[sqlite3.Row]:
    """Documents with a known page count, a live occurrence, and no page hashes yet."""
    sql = """
        SELECT d.document_id AS document_id, MIN(o.rel_path) AS rel_path
        FROM document d
        JOIN occurrence o
          ON o.document_id = d.document_id AND o.missing_since IS NULL
        WHERE d.page_count IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM page_hash ph WHERE ph.document_id = d.document_id)
        GROUP BY d.document_id
        ORDER BY d.document_id
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def ensure_page_hashes(
    conn: sqlite3.Connection, root: Path, *, now: str, limit: int | None = None
) -> tuple[int, int]:
    """Backfill page hashes for documents that lack them. Returns (documents, pages).

    Commits per document so an interrupted run resumes, matching ingest.run_ingest.
    Read-only on the document tree.
    """
    if not phash_available():
        return 0, 0
    root = Path(root)
    documents = 0
    pages = 0
    for row in pending_hash_documents(conn, limit=limit):
        path = root / row["rel_path"]
        try:
            hashes = page_phashes(path)
        except Exception:
            # Unreadable, absent, or unrenderable - the render stack raises library-private
            # exception types, so catch broadly and count it as an error in the caller.
            continue
        if not hashes:
            continue
        try:
            with conn:  # commit per document: an interrupted backfill is resumable
                conn.executemany(
                    "INSERT OR REPLACE INTO page_hash "
                    "(document_id, page_no, phash, algo, computed_at) VALUES (?, ?, ?, ?, ?)",
                    [
                        (row["document_id"], page_no, phash, PHASH_ALGO, now)
                        for page_no, phash in enumerate(hashes)
                    ],
                )
        except sqlite3.Error:
            continue
        documents += 1
        pages += len(hashes)
    return documents, pages


def exact_duplicates(conn: sqlite3.Connection) -> list[tuple[int, list[str]]]:
    """Documents held at more than one live path: (document_id, sorted rel_paths).

    Zero false positives by schema - one document_id is exactly one sha256.
    """
    groups: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT document_id, rel_path FROM occurrence "
        "WHERE missing_since IS NULL ORDER BY document_id, rel_path"
    ):
        groups.setdefault(row["document_id"], []).append(row["rel_path"])
    return [(doc_id, paths) for doc_id, paths in groups.items() if len(paths) > 1]


def _page_hashes_by_document(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """document_id -> page hashes in page order, for the current algo only."""
    pages: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT document_id, phash FROM page_hash WHERE algo = ? ORDER BY document_id, page_no",
        (PHASH_ALGO,),
    ):
        pages.setdefault(row["document_id"], []).append(row["phash"])
    return pages


def _sha256_by_document(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        row["document_id"]: row["sha256"]
        for row in conn.execute("SELECT document_id, sha256 FROM document")
    }


def match_pages(pages_a: list[str], pages_b: list[str], *, max_distance: int) -> float | None:
    """Greedy nearest-first pairing of every page of A to a distinct page of B.

    Returns the mean page distance when every page of A finds an unused page of B within
    `max_distance`, else None. Pure.
    """
    if not pages_a or len(pages_a) > len(pages_b):
        return None
    remaining = list(range(len(pages_b)))
    total = 0
    for hash_a in pages_a:
        best_index = None
        best_distance = None
        for index in remaining:
            distance = hamming_hex(hash_a, pages_b[index])
            if best_distance is None or distance < best_distance:
                best_index, best_distance = index, distance
                if distance == 0:
                    break
        if best_index is None or best_distance > max_distance:
            return None
        remaining.remove(best_index)
        total += best_distance
    return total / len(pages_a)


def pair_budget_exceeded(conn: sqlite3.Connection, *, max_pairs: int = MAX_PAIRS) -> bool:
    """True when the hashed corpus has more candidate pairs than a pass will examine."""
    count = len(_page_hashes_by_document(conn))
    return count * (count - 1) // 2 > max_pairs


def _candidate_pairs(pages: dict[int, list[str]], *, equal_length: bool) -> list[tuple[int, int]]:
    """Ordered (a, b) candidates, bucketed by page count.

    `equal_length` selects the near tier's equal-page-count buckets; otherwise the subset
    tier's strictly-smaller-into-larger pairs. Documents sharing an exact page hash are
    emitted first, so a truncated pass keeps the strongest candidates.
    """
    by_hash: dict[str, set[int]] = {}
    for doc_id, hashes in pages.items():
        for phash in hashes:
            by_hash.setdefault(phash, set()).add(doc_id)

    def keep(a: int, b: int) -> bool:
        if equal_length:
            return a < b and len(pages[a]) == len(pages[b])
        return 0 < len(pages[a]) < len(pages[b])

    seen: set[tuple[int, int]] = set()
    prefiltered: list[tuple[int, int]] = []
    for doc_ids in by_hash.values():
        if len(doc_ids) < 2:
            continue
        ordered = sorted(doc_ids)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                for pair in ((first, second), (second, first)):
                    if pair not in seen and keep(*pair):
                        seen.add(pair)
                        prefiltered.append(pair)

    rest: list[tuple[int, int]] = []
    ordered_docs = sorted(pages)
    for i, first in enumerate(ordered_docs):
        for second in ordered_docs[i + 1:]:
            for pair in ((first, second), (second, first)):
                if pair not in seen and keep(*pair):
                    seen.add(pair)
                    rest.append(pair)
    return prefiltered + rest


def near_duplicates(
    conn: sqlite3.Connection, *, max_distance: int, max_pairs: int = MAX_PAIRS
) -> list[Match]:
    """Equal-page-count documents whose pages all pair within `max_distance`."""
    pages = _page_hashes_by_document(conn)
    shas = _sha256_by_document(conn)
    matches: list[Match] = []
    for examined, pair in enumerate(_candidate_pairs(pages, equal_length=True)):
        if examined >= max_pairs:
            break
        a, b = pair
        # Defensive: distinct document rows cannot share a sha256 (UNIQUE), so this only
        # fires if that ever stops holding - tier 1 owns byte-identical documents.
        if shas.get(a) is not None and shas.get(a) == shas.get(b):
            continue
        score = match_pages(pages[a], pages[b], max_distance=max_distance)
        if score is None:
            continue
        matches.append(
            Match(
                kind="near",
                document_a=a,
                document_b=b,
                score=score,
                detail=f"{len(pages[a])} page(s), mean distance {score:.2f} <= {max_distance}",
            )
        )
    return matches


def subset_matches(
    conn: sqlite3.Connection, *, max_distance: int, max_pairs: int = MAX_PAIRS
) -> list[Match]:
    """Documents whose every page appears in a strictly longer document (A inside B)."""
    pages = _page_hashes_by_document(conn)
    matches: list[Match] = []
    for examined, pair in enumerate(_candidate_pairs(pages, equal_length=False)):
        if examined >= max_pairs:
            break
        a, b = pair
        if match_pages(pages[a], pages[b], max_distance=max_distance) is None:
            continue
        matches.append(
            Match(
                kind="subset",
                document_a=a,
                document_b=b,
                score=len(pages[a]) / len(pages[b]),
                detail=f"{len(pages[a])} of {len(pages[b])} page(s) contained",
            )
        )
    return matches


def _normalized(match: Match) -> tuple[int, int]:
    """Near pairs are stored normalized; subset order is meaningful and kept as found."""
    if match.kind == "near" and match.document_a > match.document_b:
        return match.document_b, match.document_a
    return match.document_a, match.document_b


def queue_review(conn: sqlite3.Connection, matches, *, now: str) -> int:
    """Upsert matches into the review queue. Returns the count newly inserted.

    Idempotent: a re-run refreshes score and detail but never duplicates a row and never
    reopens a human-resolved one. Nothing is ever auto-merged (docs/Architecture.md §6).
    """
    inserted = 0
    with conn:
        for match in matches:
            document_a, document_b = _normalized(match)
            row = conn.execute(
                "SELECT review_id FROM dupe_review "
                "WHERE kind = ? AND document_a = ? AND document_b = ?",
                (match.kind, document_a, document_b),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO dupe_review (kind, document_a, document_b, score, detail, "
                    "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (match.kind, document_a, document_b, match.score, match.detail, now),
                )
                inserted += 1
            else:  # status and resolved_at belong to the human, never overwritten here
                conn.execute(
                    "UPDATE dupe_review SET score = ?, detail = ? WHERE review_id = ?",
                    (match.score, match.detail, row["review_id"]),
                )
    return inserted


def pending_reviews(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    """Review rows still awaiting a verdict, strongest tier first."""
    sql = (
        "SELECT review_id, kind, document_a, document_b, score, detail, created_at "
        "FROM dupe_review WHERE status = 'pending' ORDER BY kind, document_a, document_b"
    )
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def _normalize_verdict(verdict: str) -> str:
    normalized = verdict.strip().lower().replace("-", "_")
    if normalized not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VALID_VERDICTS)}")
    return normalized


def record_label(
    conn: sqlite3.Connection,
    document_a: int,
    document_b: int,
    kind: str,
    verdict: str,
    *,
    source: str = "cli",
    now: str | None = None,
) -> str:
    """Record one human judgment in the labelled sample. Returns the stored verdict.

    One row per (document_a, document_b, kind): re-recording updates rather than
    duplicates. The matching review row, if any, takes the same status and is stamped
    resolved.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {', '.join(VALID_KINDS)}")
    normalized = _normalize_verdict(verdict)
    if kind == "near" and document_a > document_b:
        document_a, document_b = document_b, document_a
    stamp = now or _now()
    with conn:
        conn.execute(
            """
            INSERT INTO dupe_label (document_a, document_b, kind, verdict, source, labelled_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_a, document_b, kind) DO UPDATE SET
              verdict     = excluded.verdict,
              source      = excluded.source,
              labelled_at = excluded.labelled_at
            """,
            (document_a, document_b, kind, normalized, source, stamp),
        )
        conn.execute(
            "UPDATE dupe_review SET status = ?, resolved_at = ? "
            "WHERE kind = ? AND document_a = ? AND document_b = ?",
            (normalized, stamp, kind, document_a, document_b),
        )
    return normalized


def export_labels(conn: sqlite3.Connection) -> list[dict]:
    """The labelled sample as plain records, for threshold tuning."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT document_a, document_b, kind, verdict, source, labelled_at "
            "FROM dupe_label ORDER BY label_id"
        )
    ]


def run_report(
    conn: sqlite3.Connection,
    root: Path,
    *,
    max_distance: int = DEFAULT_PHASH_MAX_DISTANCE,
    now: str | None = None,
) -> DupeSummary:
    """Compute the three tiers and queue the uncertain ones.

    Writes only to the index: the backfill reads the document tree, and tiers 2 and 3
    land in `dupe_review` as 'pending'. No merge, rename, or delete path exists here
    (docs/Architecture.md §6).
    """
    db.require_migrated(conn)
    stamp = now or _now()
    root = Path(root)

    available = phash_available()
    pending_before = len(pending_hash_documents(conn)) if available else 0
    hashed_documents, hashed_pages = ensure_page_hashes(conn, root, now=stamp)
    errors = max(pending_before - hashed_documents, 0)

    exact = exact_duplicates(conn)
    near = near_duplicates(conn, max_distance=max_distance) if available else []
    subset = subset_matches(conn, max_distance=max_distance) if available else []
    queued = queue_review(conn, [*near, *subset], now=stamp)

    return DupeSummary(
        exact_groups=len(exact),
        exact_documents=sum(len(paths) for _doc_id, paths in exact),
        near=len(near),
        subset=len(subset),
        queued=queued,
        hashed_documents=hashed_documents,
        hashed_pages=hashed_pages,
        phash_available=available,
        truncated=pair_budget_exceeded(conn) if available else False,
        errors=errors,
    )
