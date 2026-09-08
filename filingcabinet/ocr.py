"""Phase 4 OCR ladder (docs/Architecture.md §5).

Per-page ladder driven by `[ocr].ladder`: `local` reads the embedded PyMuPDF text layer and
falls back to tesseract on rasterized pages; `vision` is the agent-driven last resort, marked
here and committed later by `submit_vision_text`. `drive` is reserved for the Drive-API phase
and is skipped with a recorded reason rather than treated as an error.

A page is done when its recorded confidence reaches `[ocr].min_confidence`; a page below it
resumes at the rung *after* the one recorded, so re-runs strictly advance and a page whose
ladder is spent settles at `exhausted` instead of being retried forever. The exception is a rung
that was *unavailable* rather than insufficient (no tesseract, an unimplemented rung): the reason
is recorded on the page and that rung is retried on the next run, so installing the toolchain a
`doctor` report asked for actually fixes the page. Re-escalation never blanks an indexed page: a
deferral moves the ladder bookkeeping and keeps the text already read.

Read-only on the document tree - pages are opened read-only and rasterized into an OS
temporary directory, never under [paths].root (docs/Architecture.md §6); every write lands in
the SQLite index. The toolchain is optional: a missing tesseract (or a missing PyMuPDF) makes
pages report `skipped` with a note, never raises and never fails the run.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import db

RENDER_DPI = 300
MAX_SUBMIT_CHARS = 1_000_000
TESSERACT_TIMEOUT_S = 120

DEFAULT_LADDER: tuple[str, ...] = ("local", "vision")
DEFAULT_MIN_CONFIDENCE = 0.75

# Text density at which a page's confidence stops being scaled down: a page with fewer
# characters than this is treated as a fragment, not as a read text layer.
_DENSITY_CHARS = 200

STATUS_OK = "ok"
STATUS_PENDING_VISION = "pending_vision"
STATUS_SKIPPED = "skipped"
STATUS_EXHAUSTED = "exhausted"

# Notes record *why* a rung produced nothing. An availability reason (the toolchain or the rung
# itself is missing) describes the environment, not the page, so it is retryable; a quality
# reason (a thin or garbled read) is not - that is what the strictly-advancing resume rule is for.
NOTE_TESSERACT_MISSING = "tesseract_missing"
NOTE_RUNG_UNAVAILABLE = "rung_unavailable:"

# page_ocr.ocr_source is the fine vocabulary; document.ocr_source keeps §5's coarse one.
_COARSE_SOURCE = {
    "local_text": "local",
    "local_tesseract": "local",
    "drive": "drive",
    "vision": "vision",
}
_COARSE_RANK = {"local": 0, "drive": 1, "vision": 2}

try:  # PyMuPDF is an optional extra ([ocr]/[dedup]); OCR degrades to unavailable.
    import fitz as _fitz
except Exception:  # pragma: no cover - depends on the local environment
    _fitz = None


@dataclass(frozen=True)
class OcrConfig:
    ladder: tuple[str, ...] = DEFAULT_LADDER
    min_confidence: float = DEFAULT_MIN_CONFIDENCE

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> "OcrConfig":
        """Read `[ocr]`, tolerating a missing or malformed table (config is user input)."""
        if not isinstance(mapping, dict):
            return cls()
        raw_ladder = mapping.get("ladder")
        ladder: tuple[str, ...] = ()
        if isinstance(raw_ladder, (list, tuple)):
            ladder = tuple(str(name).strip().lower() for name in raw_ladder if str(name).strip())
        try:
            min_confidence = float(mapping.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
        except (TypeError, ValueError):
            min_confidence = DEFAULT_MIN_CONFIDENCE
        return cls(
            ladder=ladder or DEFAULT_LADDER,
            min_confidence=min(1.0, max(0.0, min_confidence)),
        )


@dataclass(frozen=True)
class OcrSummary:
    documents: int = 0
    pages: int = 0
    ok: int = 0
    pending_vision: int = 0
    skipped: int = 0
    exhausted: int = 0
    # Pages whose recorded reason is an unavailable rung: the operator-facing signal that the run
    # degraded for an environment reason (KNOWN_ENV_LIMITS - tesseract may be absent) rather than
    # because the pages were unreadable. Counted alongside the statuses, never instead of them.
    degraded: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PageResult:
    text: str | None = None
    confidence: float = 0.0
    rung: str | None = None
    ocr_source: str | None = None
    status: str = STATUS_SKIPPED
    note: str | None = None


def _now() -> str:
    """UTC ISO timestamp, microsecond precision (matches ingest._now)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _merge(a: OcrSummary, b: OcrSummary) -> OcrSummary:
    return OcrSummary(
        documents=a.documents + b.documents,
        pages=a.pages + b.pages,
        ok=a.ok + b.ok,
        pending_vision=a.pending_vision + b.pending_vision,
        skipped=a.skipped + b.skipped,
        exhausted=a.exhausted + b.exhausted,
        degraded=a.degraded + b.degraded,
        errors=a.errors + b.errors,
    )


def confidence_of(text: str | None) -> float:
    """Heuristic per-page confidence in [0.0, 1.0] for text with no OCR-reported score.

    Two factors: how much of the text is legible characters (mojibake and U+FFFD drag it
    down) and how much text there is at all (a five-character fragment is not a read page).
    A clean embedded text layer scores ~1.0; an empty or garbled one falls below the default
    0.75 threshold and escalates.
    """
    if not text or not text.strip():
        return 0.0
    stripped = text.strip()
    legible = sum(
        1 for ch in stripped if ch != "�" and (ch.isspace() or ch.isprintable())
    )
    printable_ratio = legible / len(stripped)
    density = min(1.0, len(stripped) / _DENSITY_CHARS)
    return round(printable_ratio * density, 3)


def tesseract_version() -> str | None:
    """Installed tesseract version, or None when it is absent or unusable. Never raises."""
    if shutil.which("tesseract") is None:
        return None
    try:
        proc = subprocess.run(
            ["tesseract", "--version"],  # argv list, never shell=True
            capture_output=True,
            timeout=TESSERACT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    output = (proc.stdout or proc.stderr or b"").decode("utf-8", errors="replace")
    first = output.strip().splitlines()[0] if output.strip() else ""
    return first.strip() or None


def pymupdf_version() -> str | None:
    """Installed PyMuPDF version, or None when the optional extra is absent."""
    if _fitz is None:
        return None
    return str(getattr(_fitz, "__version__", "unknown"))


def parse_tesseract_tsv(tsv: str) -> tuple[str, float]:
    """(text, confidence) from tesseract's TSV output.

    The input is derived from document content, so every row is treated as untrusted:
    short, malformed, or non-numeric rows are skipped rather than raising.
    """
    lines = tsv.splitlines()
    if not lines:
        return "", 0.0
    header = lines[0].split("\t")
    try:
        conf_index = header.index("conf")
        text_index = header.index("text")
    except ValueError:  # no header: fall back to tesseract's fixed column order
        conf_index, text_index = 10, 11
    words: list[str] = []
    confidences: list[float] = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) <= max(conf_index, text_index):
            continue
        word = fields[text_index].strip()
        if not word:
            continue
        try:
            confidence = float(fields[conf_index])
        except ValueError:
            continue
        if confidence < 0:  # -1 marks layout rows, not words
            continue
        words.append(word)
        confidences.append(confidence)
    if not words:
        return "", 0.0
    return " ".join(words), round(sum(confidences) / len(confidences) / 100.0, 3)


def run_tesseract(image_path: Path) -> tuple[str, float]:
    """OCR one rendered page image. Returns ("", 0.0) rather than raising on any failure."""
    try:
        proc = subprocess.run(
            ["tesseract", str(image_path), "stdout", "tsv"],  # argv list, never shell=True
            capture_output=True,
            timeout=TESSERACT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return "", 0.0
    if proc.returncode != 0:
        return "", 0.0
    return parse_tesseract_tsv((proc.stdout or b"").decode("utf-8", errors="replace"))


def extract_text_layer(page) -> str:
    """The page's embedded text layer, or "" when it has none."""
    return page.get_text("text") or ""


def _render_page(page, tmp_dir: Path) -> Path | None:
    """Rasterize a page into ``tmp_dir``. Never writes under the document root."""
    try:
        zoom = RENDER_DPI / 72.0
        pixmap = page.get_pixmap(matrix=_fitz.Matrix(zoom, zoom))
        target = Path(tmp_dir) / f"page-{getattr(page, 'number', 0) + 1}.png"
        pixmap.save(target)
        return target
    except Exception:  # render stacks raise library-private types
        return None


def _rung_local(
    page,
    *,
    tmp_dir: Path,
    min_confidence: float,
    extractor: Callable = extract_text_layer,
    runner: Callable[[Path], tuple[str, float]] = run_tesseract,
) -> PageResult:
    """Text layer first; tesseract on the rasterized page only when the layer is too thin."""
    try:
        text = extractor(page) or ""
    except Exception:
        text = ""
    confidence = confidence_of(text)
    if confidence >= min_confidence:
        return PageResult(
            text=text,
            confidence=confidence,
            rung="local",
            ocr_source="local_text",
            status=STATUS_OK,
        )

    layer = PageResult(text=text or None, confidence=confidence, rung="local")
    if tesseract_version() is None:
        return replace(layer, status=STATUS_SKIPPED, note=NOTE_TESSERACT_MISSING)
    image_path = _render_page(page, tmp_dir)
    if image_path is None:
        return replace(layer, status=STATUS_SKIPPED, note="render_failed")

    ocr_text, ocr_confidence = runner(image_path)
    if ocr_confidence < confidence:  # keep whichever read was better
        return replace(layer, status=STATUS_SKIPPED, note="tesseract_below_text_layer")
    status = STATUS_OK if ocr_confidence >= min_confidence else STATUS_SKIPPED
    return PageResult(
        text=ocr_text or None,
        confidence=ocr_confidence,
        rung="local",
        ocr_source="local_tesseract" if ocr_text else None,
        status=status,
        note=None if ocr_text else "tesseract_empty",
    )


def _rung_vision(page, **_kwargs) -> PageResult:
    """A marker, never work: vision needs an agent round trip (`ocr submit` commits it)."""
    return PageResult(rung="vision", status=STATUS_PENDING_VISION)


RUNGS: dict[str, Callable[..., PageResult]] = {"local": _rung_local, "vision": _rung_vision}


def _row_note(row) -> str | None:
    """The row's note, tolerating a row selected without that column."""
    try:
        return row["note"]
    except (IndexError, KeyError, TypeError):
        return None


def _unavailable_rung(note: str | None) -> str | None:
    """The rung a note says could not run at all, or None for a quality reason.

    `tesseract_missing` and `rung_unavailable:<name>` are properties of the environment, so the
    page is not finished with that rung - it never got to try it.
    """
    if not note:
        return None
    if note == NOTE_TESSERACT_MISSING:
        return "local"
    if note.startswith(NOTE_RUNG_UNAVAILABLE):
        return note[len(NOTE_RUNG_UNAVAILABLE) :] or None
    return None


def _resume_index(ladder: tuple[str, ...], row: sqlite3.Row | None) -> int:
    """The ladder index to resume at: the rung after the one recorded, or 0 for a new page.

    One exception to "strictly after": a page whose recorded note names a rung that was
    *unavailable* resumes **at** that rung, so a toolchain installed since the last run is
    actually used. The re-attempt is a cheap no-op while the rung stays unavailable, and the
    walk still advances past it, so nothing loops.
    """
    if row is None or not row["rung"]:
        return 0
    blocked = _unavailable_rung(_row_note(row))
    if blocked is not None and blocked in ladder:
        return ladder.index(blocked)
    try:
        return ladder.index(row["rung"]) + 1
    except ValueError:  # the ladder changed under us: start over rather than guess
        return 0


def _page_is_done(row: sqlite3.Row | None, min_confidence: float) -> bool:
    if row is None:
        return False
    return (row["confidence"] or 0.0) >= min_confidence


def walk_ladder(
    page,
    *,
    config: OcrConfig,
    start_index: int,
    tmp_dir: Path,
    extractor: Callable = extract_text_layer,
    runner: Callable[[Path], tuple[str, float]] = run_tesseract,
) -> PageResult:
    """Run the ladder from ``start_index`` until a rung clears the bar or the ladder is spent.

    Stops at the first rung whose confidence reaches `min_confidence` (`ok`) or that defers to
    an agent (`pending_vision`). An unimplemented rung name is recorded and stepped over. When
    no rung remains, the best result seen is recorded as `exhausted` - unless the last rung
    reported an environment reason (`skipped` + note), which is kept so an operator can see it.
    """
    best = PageResult()
    have_best = False
    last: PageResult | None = None
    carry_note: str | None = None
    for name in config.ladder[start_index:]:
        rung = RUNGS.get(name)
        if rung is None:
            last = PageResult(
                rung=name, status=STATUS_SKIPPED, note=f"{NOTE_RUNG_UNAVAILABLE}{name}"
            )
            carry_note = last.note
            continue
        result = rung(
            page,
            tmp_dir=tmp_dir,
            min_confidence=config.min_confidence,
            extractor=extractor,
            runner=runner,
        )
        last = result
        if _unavailable_rung(result.note) is not None:
            carry_note = result.note
        if result.status == STATUS_PENDING_VISION:
            # Carry the best read so far, so a partial local read is not lost while the page
            # waits for an agent - and carry the reason an earlier rung could not run, so a
            # missing toolchain stays visible (and retryable) on the deferred row instead of
            # being erased by the deferral.
            if have_best:
                return replace(
                    result,
                    text=best.text,
                    confidence=best.confidence,
                    ocr_source=best.ocr_source,
                    note=carry_note or result.note,
                )
            return replace(result, note=carry_note or result.note)
        if not have_best or result.confidence > best.confidence:
            best, have_best = result, True
        if result.confidence >= config.min_confidence:
            return replace(result, status=STATUS_OK)
    if last is not None and last.status == STATUS_SKIPPED and last.note:
        return last  # an availability reason is more useful than a bare `exhausted`
    # Record the last rung attempted, so the next run resumes strictly after it.
    return replace(
        best,
        status=STATUS_EXHAUSTED,
        rung=last.rung if last is not None else best.rung,
        note=carry_note or best.note,
    )


def _keep_earlier_read(
    conn: sqlite3.Connection, document_id: int, page_number: int, result: PageResult
) -> PageResult:
    """Never blank an indexed page: a deferral or a skip changes status, not content.

    A re-escalation - the operator raised `[ocr].min_confidence`, or the ladder grew a rung -
    resumes above the rung that did the reading, so its result carries no text of its own.
    Writing that over the stored read would drop the document out of the FTS index on a config
    change alone, so the earlier text, its confidence and its source are kept and only the ladder
    bookkeeping moves.
    """
    if result.text and result.text.strip():
        return result
    row = conn.execute(
        "SELECT text, confidence, ocr_source FROM page_ocr "
        "WHERE document_id = ? AND page_number = ?",
        (document_id, page_number),
    ).fetchone()
    if row is None or not row["text"] or not str(row["text"]).strip():
        return result
    return replace(
        result,
        text=row["text"],
        confidence=max(float(result.confidence), float(row["confidence"] or 0.0)),
        ocr_source=result.ocr_source or row["ocr_source"],
    )


def _upsert_page(
    conn: sqlite3.Connection, document_id: int, page_number: int, result: PageResult, now: str
) -> None:
    result = _keep_earlier_read(conn, document_id, page_number, result)
    conn.execute(
        """
        INSERT INTO page_ocr (document_id, page_number, text, confidence, rung, ocr_source,
                              status, note, updated_at)
        VALUES (:doc, :page, :text, :conf, :rung, :source, :status, :note, :now)
        ON CONFLICT(document_id, page_number) DO UPDATE SET
          text       = excluded.text,
          confidence = excluded.confidence,
          rung       = excluded.rung,
          ocr_source = excluded.ocr_source,
          status     = excluded.status,
          note       = excluded.note,
          updated_at = excluded.updated_at
        """,
        {
            "doc": document_id,
            "page": page_number,
            "text": result.text,
            "conf": float(result.confidence),
            "rung": result.rung,
            "source": result.ocr_source,
            "status": result.status,
            "note": result.note,
            "now": now,
        },
    )


def roll_up_document(conn: sqlite3.Connection, document_id: int, now: str | None = None) -> None:
    """Fold the document's page texts into document.ocr_text in one UPDATE.

    One update per document is what keeps the FTS triggers cheap: the index is rewritten
    once per OCR pass, not once per page.
    """
    rows = list(
        conn.execute(
            "SELECT text, ocr_source FROM page_ocr WHERE document_id = ? ORDER BY page_number",
            (document_id,),
        )
    )
    texts = [row["text"] for row in rows if row["text"] and row["text"].strip()]
    coarse = [
        _COARSE_SOURCE[row["ocr_source"]] for row in rows if row["ocr_source"] in _COARSE_SOURCE
    ]
    source = max(coarse, key=lambda name: _COARSE_RANK[name]) if coarse else None
    conn.execute(
        "UPDATE document SET ocr_text = ?, ocr_source = ?, updated_at = ? WHERE document_id = ?",
        ("\n\n".join(texts) if texts else None, source, now or _now(), document_id),
    )


def ocr_document(
    conn: sqlite3.Connection,
    document_id: int,
    path: Path,
    *,
    config: OcrConfig,
    now: str | None = None,
    extractor: Callable = extract_text_layer,
    runner: Callable[[Path], tuple[str, float]] = run_tesseract,
) -> OcrSummary:
    """Walk the ladder over every page of one document. Read-only on ``path``."""
    if _fitz is None:  # pragma: no cover - depends on the local environment
        return OcrSummary(skipped=1)
    stamp = now or _now()
    existing = {
        row["page_number"]: row
        for row in conn.execute(
            "SELECT page_number, confidence, rung, status, note FROM page_ocr "
            "WHERE document_id = ?",
            (document_id,),
        )
    }
    counts = {STATUS_OK: 0, STATUS_PENDING_VISION: 0, STATUS_SKIPPED: 0, STATUS_EXHAUSTED: 0}
    pages = 0
    degraded = 0
    with tempfile.TemporaryDirectory(prefix="fc-ocr-") as tmp:
        tmp_dir = Path(tmp)  # never under [paths].root (docs/Architecture.md §6)
        with _fitz.open(path) as document:
            for index, page in enumerate(document):
                page_number = index + 1
                row = existing.get(page_number)
                start_index = _resume_index(config.ladder, row)
                if _page_is_done(row, config.min_confidence) or start_index >= len(config.ladder):
                    continue  # done, or the ladder is spent: never retried
                result = walk_ladder(
                    page,
                    config=config,
                    start_index=start_index,
                    tmp_dir=tmp_dir,
                    extractor=extractor,
                    runner=runner,
                )
                with conn:  # commit per page: an interrupted run resumes where it stopped
                    _upsert_page(conn, document_id, page_number, result, stamp)
                pages += 1
                counts[result.status] = counts.get(result.status, 0) + 1
                if _unavailable_rung(result.note) is not None:
                    degraded += 1
    if pages:
        with conn:
            roll_up_document(conn, document_id, stamp)
    return OcrSummary(
        documents=1,
        pages=pages,
        ok=counts[STATUS_OK],
        pending_vision=counts[STATUS_PENDING_VISION],
        skipped=counts[STATUS_SKIPPED],
        exhausted=counts[STATUS_EXHAUSTED],
        degraded=degraded,
    )


def pending_documents(
    conn: sqlite3.Connection,
    *,
    document_ids: Iterable[int] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Documents with at least one live occurrence: (document_id, one rel_path)."""
    sql = """
        SELECT d.document_id AS document_id, MIN(o.rel_path) AS rel_path
        FROM document d
        JOIN occurrence o
          ON o.document_id = d.document_id AND o.missing_since IS NULL
    """
    params: list = []
    ids = list(document_ids) if document_ids is not None else None
    if ids is not None:
        if not ids:
            return []
        sql += f" WHERE d.document_id IN ({','.join('?' for _ in ids)})"
        params.extend(int(document_id) for document_id in ids)
    sql += " GROUP BY d.document_id ORDER BY d.document_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return list(conn.execute(sql, params))


def run_ocr(
    conn: sqlite3.Connection,
    root: Path,
    *,
    config: OcrConfig | None = None,
    document_ids: Iterable[int] | None = None,
    limit: int | None = None,
    now: str | None = None,
    extractor: Callable = extract_text_layer,
    runner: Callable[[Path], tuple[str, float]] = run_tesseract,
) -> OcrSummary:
    """Run the ladder over every live document under ``root``.

    Degrades rather than raises: without PyMuPDF every candidate is reported skipped, and an
    unreadable or unopenable document is counted in `errors` while the run carries on.
    """
    db.require_migrated(conn)
    config = config or OcrConfig()
    root = Path(root).resolve()
    candidates = pending_documents(conn, document_ids=document_ids, limit=limit)
    if _fitz is None:  # pragma: no cover - depends on the local environment
        return OcrSummary(skipped=len(candidates))

    summary = OcrSummary()
    for row in candidates:
        path = root / row["rel_path"]
        try:
            # Same guard as ingest.run_ingest: a rel_path was inside the root at scan time,
            # but a symlink planted since must never make us read outside it.
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            summary = _merge(summary, OcrSummary(errors=1))
            continue
        try:
            summary = _merge(
                summary,
                ocr_document(
                    conn,
                    row["document_id"],
                    path,
                    config=config,
                    now=now,
                    extractor=extractor,
                    runner=runner,
                ),
            )
        except (OSError, sqlite3.Error):
            summary = _merge(summary, OcrSummary(errors=1))
        except Exception:  # unopenable/corrupt document: the render stack raises private types
            summary = _merge(summary, OcrSummary(errors=1))
    return summary


def submit_vision_text(
    conn: sqlite3.Connection,
    document_id: int,
    page_number: int,
    text: str,
    *,
    now: str | None = None,
) -> dict:
    """Commit agent-supplied text for one page as the terminal `vision` rung.

    The text is untrusted input: it is length-capped, bound as a SQL parameter, and never
    interpreted as a path or a shell argument. Raises ValueError on anything invalid.
    """
    db.require_migrated(conn)
    row = conn.execute(
        "SELECT page_count FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no document {document_id}")
    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page must be a 1-based page number")
    page_count = row["page_count"]
    if page_count is not None and page_number > page_count:
        raise ValueError(f"page {page_number} is beyond the document's {page_count} page(s)")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("submitted text is empty")
    if len(text) > MAX_SUBMIT_CHARS:
        raise ValueError(f"submitted text exceeds {MAX_SUBMIT_CHARS} characters")

    stamp = now or _now()
    result = PageResult(
        text=text,
        confidence=1.0,
        rung="vision",
        ocr_source="vision",
        status=STATUS_OK,
    )
    with conn:
        _upsert_page(conn, document_id, page_number, result, stamp)
        roll_up_document(conn, document_id, stamp)
    return {
        "document_id": document_id,
        "page": page_number,
        "ocr_source": "vision",
        "chars": len(text),
    }
