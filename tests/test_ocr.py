import hashlib
import shutil
import time
from pathlib import Path

import pytest

from filingcabinet import db, ocr, search

NOW = "2026-01-01T00:00:00.000000+00:00"

# 200+ characters of ordinary prose: dense and fully legible, so it clears the default
# confidence bar without any OCR pass.
CLEAN_TEXT = (
    "Invoice 2026-014 from Northwind Supplies, dated 3 February 2026, for office "
    "consumables delivered to the Camden depot. Payment is due within thirty days of "
    "the invoice date by bank transfer to the account named on this page."
)


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def _add_document(conn, sha: str, *, page_count: int | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, page_count, first_seen_at, updated_at) "
        "VALUES (?, 1, ?, ?, ?)",
        (sha, page_count, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _add_occurrence(conn, document_id: int, rel_path: str, *, missing: bool = False) -> None:
    conn.execute(
        "INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at, "
        "missing_since) VALUES (?, ?, 0.0, 1, ?, ?)",
        (document_id, rel_path, NOW, NOW if missing else None),
    )


def _pages(conn, document_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT page_number, text, confidence, rung, ocr_source, status, note "
            "FROM page_ocr WHERE document_id = ? ORDER BY page_number",
            (document_id,),
        )
    ]


# The ladder walk itself needs a real page object only to hand to the rungs; PyMuPDF is the
# optional `ocr` extra, so every test that opens a document is skipped where it is absent.
requires_pymupdf = pytest.mark.skipif(
    ocr.pymupdf_version() is None, reason="optional `ocr` extra (PyMuPDF)"
)
requires_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is not installed on this host"
)


def _write_pdf(path: Path, pages: int = 1, lines: list[str] | None = None) -> None:
    import fitz

    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        if lines is not None and index < len(lines):
            page.insert_text((72, 100), lines[index])
    doc.save(path)
    doc.close()


@pytest.fixture()
def indexed(conn, tmp_path):
    """One indexed single-page document with a real (blank) PDF behind it."""
    root = tmp_path / "docs"
    root.mkdir()
    _write_pdf(root / "a.pdf")
    document_id = _add_document(conn, "sha-a", page_count=1)
    _add_occurrence(conn, document_id, "a.pdf")
    return root, document_id


def _never_called(*_args, **_kwargs):
    pytest.fail("the tesseract runner should not have been invoked")


def test_confidence_of_empty_and_clean_text():
    assert ocr.confidence_of("") == 0.0
    assert ocr.confidence_of("   \n\t ") == 0.0
    assert ocr.confidence_of(CLEAN_TEXT) >= 0.75
    assert ocr.confidence_of("Total") < 0.75  # a fragment is not a read page
    assert ocr.confidence_of("�" * 400) < 0.75  # mojibake is not a read page


def test_ocr_config_tolerates_a_malformed_table():
    assert ocr.OcrConfig.from_mapping(None) == ocr.OcrConfig()
    assert ocr.OcrConfig.from_mapping({}) == ocr.OcrConfig()
    assert ocr.OcrConfig.from_mapping({"ladder": "local"}).ladder == ocr.DEFAULT_LADDER
    assert ocr.OcrConfig.from_mapping({"min_confidence": "nope"}).min_confidence == 0.75
    assert ocr.OcrConfig.from_mapping({"min_confidence": 9}).min_confidence == 1.0
    assert ocr.OcrConfig.from_mapping({"min_confidence": -1}).min_confidence == 0.0
    assert ocr.OcrConfig.from_mapping({"ladder": ["Drive", " local "]}).ladder == (
        "drive",
        "local",
    )


def test_parse_tesseract_tsv_tolerates_malformed_rows():
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight"
            "\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t95.5\tInvoice",
            "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t-1\t ",  # layout row: no word, no confidence
            "truncated",
            "5\t1\t1\t1\t1\t3\t0\t0\t1\t1\tnot-a-number\tskipped",
            "5\t1\t1\t1\t1\t4\t0\t0\t1\t1\t85.5\t2026",
        ]
    )
    text, confidence = ocr.parse_tesseract_tsv(tsv)
    assert text == "Invoice 2026"
    assert confidence == pytest.approx(0.905)
    assert ocr.parse_tesseract_tsv("") == ("", 0.0)


@requires_pymupdf
def test_ladder_stops_at_local_when_confident(conn, indexed):
    root, document_id = indexed
    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local", "vision"), min_confidence=0.75),
        extractor=lambda page: CLEAN_TEXT,
        runner=_never_called,
        now=NOW,
    )
    assert (summary.documents, summary.pages, summary.ok) == (1, 1, 1)
    page = _pages(conn, document_id)[0]
    assert (page["status"], page["rung"], page["ocr_source"]) == ("ok", "local", "local_text")
    assert page["confidence"] >= 0.75
    stored = conn.execute(
        "SELECT ocr_text, ocr_source FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    assert stored["ocr_text"] == CLEAN_TEXT and stored["ocr_source"] == "local"


@requires_pymupdf
def test_ladder_escalates_to_tesseract_then_pending_vision(conn, indexed, monkeypatch):
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    monkeypatch.setattr(ocr, "_render_page", lambda page, tmp_dir: Path(tmp_dir) / "page-1.png")
    calls: list[Path] = []

    def runner(image_path):
        calls.append(image_path)
        return "sm dged tex", 0.3

    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local", "vision"), min_confidence=0.75),
        extractor=lambda page: "",
        runner=runner,
        now=NOW,
    )
    assert (summary.pages, summary.pending_vision, summary.ok) == (1, 1, 0)
    assert len(calls) == 1  # the local rung really did try tesseract before deferring
    page = _pages(conn, document_id)[0]
    assert page["status"] == "pending_vision" and page["rung"] == "vision"
    assert page["text"] == "sm dged tex"  # the partial local read is carried, not lost


@requires_pymupdf
def test_rerun_skips_confident_pages(conn, tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    _write_pdf(root / "two.pdf", pages=2)
    document_id = _add_document(conn, "sha-two", page_count=2)
    _add_occurrence(conn, document_id, "two.pdf")
    monkeypatch.setattr(ocr, "tesseract_version", lambda: None)
    config = ocr.OcrConfig(ladder=("local",), min_confidence=0.75)

    first = ocr.run_ocr(
        conn,
        root,
        config=config,
        extractor=lambda page: CLEAN_TEXT if page.number == 0 else "",
        runner=_never_called,
        now=NOW,
    )
    assert (first.pages, first.ok, first.skipped) == (2, 1, 1)

    def extractor(page):
        if page.number == 0:
            pytest.fail("a page at/above min_confidence was re-read")
        return ""

    # Page 1 is done and never re-read; page 2 was skipped because tesseract is absent, which
    # is an environment reason, so it stays retryable rather than being written off.
    second = ocr.run_ocr(conn, root, config=config, extractor=extractor, now=NOW)
    assert (second.pages, second.skipped, second.degraded) == (1, 1, 1)
    assert _pages(conn, document_id)[0]["status"] == "ok"


@requires_pymupdf
def test_sub_threshold_page_re_escalates_to_the_next_rung(conn, indexed, monkeypatch):
    """A rung that ran and read too little is finished with: the next pass resumes above it."""
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    monkeypatch.setattr(ocr, "_render_page", lambda page, tmp_dir: Path(tmp_dir) / "page-1.png")
    ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local",), min_confidence=0.75),
        extractor=lambda page: "",
        runner=lambda image_path: ("blurry", 0.2),
        now=NOW,
    )
    assert _pages(conn, document_id)[0]["rung"] == "local"

    # The operator adds the vision rung: the page resumes *after* local, never from the top.
    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local", "vision"), min_confidence=0.75),
        extractor=_never_called,
        now=NOW,
    )
    assert (summary.pages, summary.pending_vision) == (1, 1)
    page = _pages(conn, document_id)[0]
    assert page["rung"] == "vision"
    assert page["text"] == "blurry"  # the read that got this far is kept, not blanked


@requires_pymupdf
def test_tesseract_absent_under_the_default_ladder_keeps_the_reason(conn, indexed, monkeypatch):
    """The deferral to vision must not erase why the local rung produced nothing.

    `[ocr].ladder` ships as ("local", "vision"), so this - not a single-rung ladder - is what an
    operator on a tesseract-less host actually runs.
    """
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: None)
    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(),  # the shipped default ladder
        extractor=lambda page: "",
        runner=_never_called,
        now=NOW,
    )
    assert (summary.pages, summary.pending_vision, summary.degraded) == (1, 1, 1)
    assert summary.errors == 0
    page = _pages(conn, document_id)[0]
    assert (page["status"], page["rung"], page["note"]) == (
        "pending_vision",
        "vision",
        "tesseract_missing",
    )


@requires_pymupdf
def test_page_skipped_for_a_missing_toolchain_recovers_when_it_appears(
    conn, indexed, monkeypatch
):
    """`doctor` tells the operator to install tesseract; the re-run after they do must read the
    page instead of reporting nothing to do."""
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: None)
    ocr.run_ocr(conn, root, config=ocr.OcrConfig(), extractor=lambda page: "", now=NOW)
    assert _pages(conn, document_id)[0]["note"] == "tesseract_missing"

    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    monkeypatch.setattr(ocr, "_render_page", lambda page, tmp_dir: Path(tmp_dir) / "page-1.png")
    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(),
        extractor=lambda page: "",
        runner=lambda image_path: (CLEAN_TEXT, 0.92),
        now=NOW,
    )
    assert (summary.pages, summary.ok, summary.degraded) == (1, 1, 0)
    page = _pages(conn, document_id)[0]
    assert (page["status"], page["rung"], page["ocr_source"]) == (
        "ok",
        "local",
        "local_tesseract",
    )
    assert page["text"] == CLEAN_TEXT


@requires_pymupdf
def test_raising_min_confidence_keeps_the_indexed_text(conn, indexed, monkeypatch):
    """Re-escalation changes a page's status, never its content: a config change alone must not
    drop a document out of the search index."""
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: None)
    text = CLEAN_TEXT[:150]  # dense enough to clear 0.5, not dense enough to clear 0.99
    ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local", "vision"), min_confidence=0.5),
        extractor=lambda page: text,
        runner=_never_called,
        now=NOW,
    )
    assert _pages(conn, document_id)[0]["status"] == "ok"
    assert [hit.document_id for hit in search.find(conn, "northwind")] == [document_id]

    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local", "vision"), min_confidence=0.99),
        extractor=_never_called,
        now=NOW,
    )
    assert (summary.pages, summary.pending_vision) == (1, 1)
    page = _pages(conn, document_id)[0]
    assert page["status"] == "pending_vision" and page["text"] == text
    stored = conn.execute(
        "SELECT ocr_text, ocr_source FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    assert stored["ocr_text"] == text and stored["ocr_source"] == "local"
    assert [hit.document_id for hit in search.find(conn, "northwind")] == [document_id]


@requires_pymupdf
def test_exhausted_page_is_not_retried(conn, indexed, monkeypatch):
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    monkeypatch.setattr(ocr, "_render_page", lambda page, tmp_dir: Path(tmp_dir) / "page-1.png")
    config = ocr.OcrConfig(ladder=("local",), min_confidence=0.75)

    first = ocr.run_ocr(
        conn,
        root,
        config=config,
        extractor=lambda page: "",
        runner=lambda image_path: ("blurry", 0.2),
        now=NOW,
    )
    assert (first.pages, first.exhausted) == (1, 1)
    assert _pages(conn, document_id)[0]["status"] == "exhausted"

    second = ocr.run_ocr(conn, root, config=config, extractor=_never_called, now=NOW)
    assert second.pages == 0
    assert _pages(conn, document_id)[0]["status"] == "exhausted"


@requires_pymupdf
def test_unknown_rung_is_skipped_not_fatal(conn, indexed, monkeypatch):
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: None)
    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("cloud", "local"), min_confidence=0.75),
        extractor=lambda page: CLEAN_TEXT,
        runner=_never_called,
        now=NOW,
    )
    assert summary.ok == 1  # the unimplemented `cloud` rung was stepped over, local still ran
    assert _pages(conn, document_id)[0]["rung"] == "local"


@requires_pymupdf
def test_tesseract_absent_degrades(conn, indexed, monkeypatch):
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: None)
    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local",), min_confidence=0.75),
        extractor=lambda page: "",
        runner=_never_called,
        now=NOW,
    )
    assert (summary.skipped, summary.errors) == (1, 0)
    page = _pages(conn, document_id)[0]
    assert (page["status"], page["note"]) == ("skipped", "tesseract_missing")


@requires_pymupdf
def test_run_ocr_skips_a_path_outside_the_root(conn, tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    document_id = _add_document(conn, "sha-out", page_count=1)
    _add_occurrence(conn, document_id, "../outside.pdf")
    monkeypatch.setattr(
        ocr, "ocr_document", lambda *a, **k: pytest.fail("read a file outside the root")
    )
    summary = ocr.run_ocr(conn, root, config=ocr.OcrConfig(), now=NOW)
    assert (summary.documents, summary.errors) == (0, 1)


def test_submit_vision_commits_and_rolls_up(conn):
    document_id = _add_document(conn, "sha-v", page_count=2)
    result = ocr.submit_vision_text(conn, document_id, 1, "handwritten note", now=NOW)
    assert result == {
        "document_id": document_id,
        "page": 1,
        "ocr_source": "vision",
        "chars": len("handwritten note"),
    }
    row = conn.execute(
        "SELECT ocr_text, ocr_source FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    assert row["ocr_text"] == "handwritten note" and row["ocr_source"] == "vision"
    page = _pages(conn, document_id)[0]
    assert (page["status"], page["confidence"], page["rung"]) == ("ok", 1.0, "vision")


def test_submit_vision_rejects_bad_input(conn):
    document_id = _add_document(conn, "sha-bad", page_count=2)
    with pytest.raises(ValueError):
        ocr.submit_vision_text(conn, document_id, 1, "   ", now=NOW)
    with pytest.raises(ValueError):
        ocr.submit_vision_text(conn, document_id, 1, "x" * (ocr.MAX_SUBMIT_CHARS + 1), now=NOW)
    with pytest.raises(ValueError):
        ocr.submit_vision_text(conn, document_id, 0, "text", now=NOW)
    with pytest.raises(ValueError):
        ocr.submit_vision_text(conn, document_id, 3, "text", now=NOW)  # beyond page_count
    with pytest.raises(ValueError):
        ocr.submit_vision_text(conn, document_id + 999, 1, "text", now=NOW)
    assert _pages(conn, document_id) == []


def test_submit_vision_text_is_never_interpreted_as_sql(conn):
    document_id = _add_document(conn, "sha-inj", page_count=1)
    payload = "'); DROP TABLE document; --"
    ocr.submit_vision_text(conn, document_id, 1, payload, now=NOW)
    assert _pages(conn, document_id)[0]["text"] == payload
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def _tree_fingerprint(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): (
            p.stat().st_size,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@requires_pymupdf
def test_ocr_leaves_document_tree_untouched(conn, tmp_path):
    """docs/Architecture.md §6: OCR reads documents and writes only the index."""
    root = tmp_path / "docs"
    root.mkdir()
    _write_pdf(root / "letter.pdf", pages=1, lines=[CLEAN_TEXT])
    document_id = _add_document(conn, "sha-letter", page_count=1)
    _add_occurrence(conn, document_id, "letter.pdf")

    before = _tree_fingerprint(root)
    summary = ocr.run_ocr(conn, root, config=ocr.OcrConfig(), now=NOW)
    assert summary.documents == 1
    assert _tree_fingerprint(root) == before  # nothing rendered, moved, or written under root


@requires_pymupdf
@requires_tesseract
def test_local_rung_reads_a_rasterized_page_with_real_tesseract(conn, tmp_path):
    """Opt-in end-to-end of the real toolchain: no text layer, so tesseract must do the work."""
    import fitz

    root = tmp_path / "docs"
    root.mkdir()
    source = fitz.open()
    page = source.new_page()
    page.insert_text((72, 100), "INVOICE 2026", fontsize=48)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(3, 3))
    rasterized = fitz.open()
    image_page = rasterized.new_page(width=page.rect.width, height=page.rect.height)
    image_page.insert_image(image_page.rect, pixmap=pixmap)
    rasterized.save(root / "scan.pdf")
    rasterized.close()
    source.close()

    document_id = _add_document(conn, "sha-scan", page_count=1)
    _add_occurrence(conn, document_id, "scan.pdf")
    ocr.run_ocr(conn, root, config=ocr.OcrConfig(ladder=("local",)), now=NOW)
    stored = conn.execute(
        "SELECT ocr_text FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()["ocr_text"]
    assert stored and "INVOICE" in stored.upper()


# --- drive rung (agent-driven, document-level) --------------------------------------------

DRIVE_TEXT = (
    "Statement of account for February 2026, Northwind Supplies, reference NW-2026-014."
)


def _add_page(
    conn,
    document_id: int,
    page_number: int,
    *,
    status: str,
    text: str | None = None,
    confidence: float = 0.0,
    rung: str | None = None,
    ocr_source: str | None = None,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO page_ocr (document_id, page_number, text, confidence, rung, ocr_source, "
        "status, note, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (document_id, page_number, text, confidence, rung, ocr_source, status, note, NOW),
    )


def test_strip_drive_trailer_only_strips_a_trailer():
    assert ocr.strip_drive_trailer("body\n\nImage labels: [receipt, paper]\n") == "body"
    assert ocr.strip_drive_trailer("Image labels: [receipt]") == ""
    # An interior mention is document content, not Drive's annotation.
    interior = "Image labels: [a]\nthe real last line"
    assert ocr.strip_drive_trailer(interior) == interior
    assert ocr.strip_drive_trailer("body") == "body"


def test_strip_drive_trailer_runs_linearly():
    """Drive text is untrusted, so the trailer scan must not backtrack.

    A long whitespace run before a non-matching tail is the pathological shape: with greedy
    quantifiers the engine retries every split of the run and the cost is quadratic (about 3.5 s
    at 32k spaces, an hour at the MAX_SUBMIT_CHARS cap). The bound below is deliberately loose -
    it is a regression trip-wire on the shape of the pattern, not a benchmark.
    """
    run = " " * 200_000
    pathological = "Image labels: x" + run + "\ny"

    started = time.perf_counter()
    assert ocr.strip_drive_trailer(pathological) == pathological
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"strip_drive_trailer backtracked: {elapsed:.1f}s on 200k spaces"

    # The same run in a position that *does* match is stripped, and just as promptly.
    started = time.perf_counter()
    assert ocr.strip_drive_trailer("body\nImage labels: [a]" + run) == "body"
    assert time.perf_counter() - started < 2.0


def test_submit_drive_marks_every_pending_page(conn):
    document_id = _add_document(conn, "sha-drive-3", page_count=3)
    _add_page(conn, document_id, 1, status="pending_vision", rung="vision")
    _add_page(
        conn,
        document_id,
        2,
        status="ok",
        text="page two local text",
        confidence=0.9,
        rung="local",
        ocr_source="local_text",
    )
    _add_page(
        conn,
        document_id,
        3,
        status="pending_vision",
        text="page three scraps",
        confidence=0.3,
        rung="vision",
        ocr_source="local_tesseract",
    )

    result = ocr.submit_drive_text(conn, document_id, DRIVE_TEXT, now=NOW)
    assert result == {
        "document_id": document_id,
        "pages_marked": 2,
        "text_page": 1,
        "ocr_source": "drive",
        "chars": len(DRIVE_TEXT),
        "empty": False,
    }

    pages = {page["page_number"]: page for page in _pages(conn, document_id)}
    assert pages[1]["text"] == DRIVE_TEXT  # the blob lands on the lowest-numbered pending page
    assert (pages[1]["status"], pages[1]["rung"], pages[1]["ocr_source"]) == (
        "ok",
        "drive",
        "drive",
    )
    assert pages[2] == {  # a page already done is never a target
        "page_number": 2,
        "text": "page two local text",
        "confidence": 0.9,
        "rung": "local",
        "ocr_source": "local_text",
        "status": "ok",
        "note": None,
    }
    # The carrier page moves to the drive rung but keeps the text it already had.
    assert pages[3]["text"] == "page three scraps"
    assert (pages[3]["status"], pages[3]["rung"], pages[3]["ocr_source"]) == (
        "ok",
        "drive",
        "drive",
    )
    stored = conn.execute(
        "SELECT ocr_text, ocr_source FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    assert stored["ocr_source"] == "drive" and DRIVE_TEXT in stored["ocr_text"]


def test_submit_drive_rescues_pending_vision_pages(conn):
    """The live corpus's stuck pages predate the rung: they are recorded `pending_vision`."""
    document_id = _add_document(conn, "sha-drive-rescue", page_count=1)
    _add_page(conn, document_id, 1, status="pending_vision", rung="vision")
    result = ocr.submit_drive_text(conn, document_id, DRIVE_TEXT, now=NOW)
    assert (result["pages_marked"], result["text_page"]) == (1, 1)
    assert _pages(conn, document_id)[0]["ocr_source"] == "drive"


def test_submit_drive_strips_image_labels_trailer(conn):
    document_id = _add_document(conn, "sha-drive-trailer", page_count=1)
    _add_page(conn, document_id, 1, status="pending_drive", rung="drive")
    ocr.submit_drive_text(
        conn, document_id, f"{DRIVE_TEXT}\n\nImage labels: [document, paper, text]\n", now=NOW
    )
    assert _pages(conn, document_id)[0]["text"] == DRIVE_TEXT


@pytest.mark.parametrize("payload", ["", "   \n\t ", "\nImage labels: [receipt, paper]\n"])
def test_submit_drive_empty_keeps_local_text(conn, payload):
    """Drive OCR is opportunistic: an empty answer must not blank a low-confidence local read."""
    document_id = _add_document(conn, f"sha-drive-empty-{len(payload)}", page_count=1)
    _add_page(
        conn,
        document_id,
        1,
        status="pending_drive",
        text="weak local read",
        confidence=0.535,
        rung="drive",
        ocr_source="local_tesseract",
    )
    result = ocr.submit_drive_text(conn, document_id, payload, now=NOW)
    assert result["empty"] is True and result["pages_marked"] == 1
    page = _pages(conn, document_id)[0]
    assert (page["text"], page["confidence"], page["ocr_source"]) == (
        "weak local read",
        0.535,
        "local_tesseract",
    )
    assert (page["status"], page["rung"], page["note"]) == ("skipped", "drive", "drive_empty")


def test_submit_drive_is_idempotent(conn):
    document_id = _add_document(conn, "sha-drive-idem", page_count=1)
    _add_page(conn, document_id, 1, status="pending_drive", rung="drive")
    ocr.submit_drive_text(conn, document_id, DRIVE_TEXT, now=NOW)
    before = _pages(conn, document_id)
    result = ocr.submit_drive_text(conn, document_id, DRIVE_TEXT, now=NOW)
    assert (result["pages_marked"], result["text_page"]) == (0, None)
    assert _pages(conn, document_id) == before


def test_submit_drive_rejects_bad_input(conn):
    document_id = _add_document(conn, "sha-drive-bad", page_count=1)
    _add_page(conn, document_id, 1, status="pending_drive", rung="drive")
    with pytest.raises(ValueError):
        ocr.submit_drive_text(conn, document_id + 999, "text", now=NOW)
    with pytest.raises(ValueError):
        ocr.submit_drive_text(conn, document_id, "x" * (ocr.MAX_SUBMIT_CHARS + 1), now=NOW)
    with pytest.raises(ValueError):
        ocr.submit_drive_text(conn, document_id, None, now=NOW)
    assert _pages(conn, document_id)[0]["status"] == "pending_drive"


def test_submit_drive_text_is_never_interpreted_as_sql(conn):
    document_id = _add_document(conn, "sha-drive-inj", page_count=1)
    _add_page(conn, document_id, 1, status="pending_drive", rung="drive")
    payload = "'); DROP TABLE document; --"
    ocr.submit_drive_text(conn, document_id, payload, now=NOW)
    assert _pages(conn, document_id)[0]["text"] == payload
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def test_submit_drive_writes_nothing_under_root(conn, tmp_path):
    """docs/Architecture.md sections 6/9: the drive path persists to SQLite only."""
    root = tmp_path / "docs"
    (root / "scans").mkdir(parents=True)
    (root / "scans" / "bill.pdf").write_bytes(b"not really a pdf")
    document_id = _add_document(conn, "sha-drive-root", page_count=1)
    _add_occurrence(conn, document_id, "scans/bill.pdf")
    _add_page(conn, document_id, 1, status="pending_drive", rung="drive")

    before = _tree_fingerprint(root)
    ocr.submit_drive_text(conn, document_id, DRIVE_TEXT, now=NOW)
    assert _tree_fingerprint(root) == before


def test_resolve_document_by_sha256_and_rel_path(conn):
    digest = "ab" * 32
    document_id = _add_document(conn, digest, page_count=1)
    _add_occurrence(conn, document_id, "invoices/2026/bill.pdf")

    assert ocr.resolve_document(conn, document_id=document_id) == document_id
    assert ocr.resolve_document(conn, sha256=digest.upper()) == document_id
    assert ocr.resolve_document(conn, rel_path="invoices/2026/bill.pdf") == document_id
    assert ocr.resolve_document(conn, rel_path=r".\invoices\2026\bill.pdf") == document_id
    assert ocr.resolve_document(conn, rel_path="2026/bill.pdf") == document_id  # path suffix

    for kwargs in (
        {},
        {"document_id": document_id, "sha256": digest},
        {"sha256": "not-hex"},
        {"sha256": "cd" * 32},
        {"document_id": document_id + 999},
        {"rel_path": "elsewhere/bill.pdf"},
        {"rel_path": ""},
    ):
        with pytest.raises(ValueError):
            ocr.resolve_document(conn, **kwargs)


def test_resolve_document_rejects_bare_filename_and_ambiguous_suffix(conn):
    first = _add_document(conn, "sha-amb-1", page_count=1)
    second = _add_document(conn, "sha-amb-2", page_count=1)
    third = _add_document(conn, "sha-amb-3", page_count=1)
    _add_occurrence(conn, first, "one/sub/bill.pdf")
    _add_occurrence(conn, second, "two/sub/bill.pdf")
    _add_occurrence(conn, third, "three/only/other.pdf")

    with pytest.raises(ValueError, match="bare filename"):
        ocr.resolve_document(conn, rel_path="bill.pdf")
    with pytest.raises(ValueError, match="matches 2 occurrences"):
        ocr.resolve_document(conn, rel_path="sub/bill.pdf")
    assert ocr.resolve_document(conn, rel_path="only/other.pdf") == third


def test_resolve_document_suffix_wildcards_are_literal(conn):
    """`_` and `%` are LIKE wildcards; an unescaped one would match the wrong document."""
    literal = _add_document(conn, "sha-like-1", page_count=1)
    other = _add_document(conn, "sha-like-2", page_count=1)
    _add_occurrence(conn, literal, "deep/scans/a_b.pdf")
    _add_occurrence(conn, other, "deep/scans/axb.pdf")
    assert ocr.resolve_document(conn, rel_path="scans/a_b.pdf") == literal


@requires_pymupdf
def test_ladder_defers_to_drive_before_vision(conn, tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    _write_pdf(root / "one.pdf")
    _write_pdf(root / "two.pdf")
    first = _add_document(conn, "sha-drive-ladder-1", page_count=1)
    _add_occurrence(conn, first, "one.pdf")
    second = _add_document(conn, "sha-drive-ladder-2", page_count=1)
    _add_occurrence(conn, second, "two.pdf")
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    monkeypatch.setattr(ocr, "_render_page", lambda page, tmp_dir: Path(tmp_dir) / "page-1.png")

    summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(ladder=("local", "drive", "vision"), min_confidence=0.75),
        document_ids=[first],
        extractor=lambda page: "",
        runner=lambda image_path: ("weak", 0.2),
        now=NOW,
    )
    assert (summary.pending_drive, summary.pending_vision) == (1, 0)
    page = _pages(conn, first)[0]
    assert (page["status"], page["rung"]) == ("pending_drive", "drive")
    assert page["text"] == "weak"  # the local read is carried, not lost

    # The default ladder has no drive rung: the same shape of page still defers to vision.
    default_summary = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(min_confidence=0.75),
        document_ids=[second],
        extractor=lambda page: "",
        runner=lambda image_path: ("weak", 0.2),
        now=NOW,
    )
    assert (default_summary.pending_drive, default_summary.pending_vision) == (0, 1)
    assert _pages(conn, second)[0]["status"] == "pending_vision"


@requires_pymupdf
def test_pending_drive_page_is_not_escalated_by_a_rerun(conn, indexed, monkeypatch):
    """Without the sticky rule the rung would look wired up and never collect a submission."""
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    monkeypatch.setattr(ocr, "_render_page", lambda page, tmp_dir: Path(tmp_dir) / "page-1.png")
    with_drive = ocr.OcrConfig(ladder=("local", "drive", "vision"), min_confidence=0.75)
    ocr.run_ocr(
        conn,
        root,
        config=with_drive,
        extractor=lambda page: "",
        runner=lambda image_path: ("weak", 0.2),
        now=NOW,
    )
    assert _pages(conn, document_id)[0]["status"] == "pending_drive"

    rerun = ocr.run_ocr(conn, root, config=with_drive, extractor=_never_called, now=NOW)
    assert rerun.pages == 0
    assert _pages(conn, document_id)[0]["status"] == "pending_drive"

    # Dropping `drive` from the ladder is the escape hatch: the page restarts and defers again.
    unstuck = ocr.run_ocr(
        conn,
        root,
        config=ocr.OcrConfig(min_confidence=0.75),
        extractor=lambda page: "",
        runner=lambda image_path: ("weak", 0.2),
        now=NOW,
    )
    assert unstuck.pending_vision == 1
    assert _pages(conn, document_id)[0]["status"] == "pending_vision"


@requires_pymupdf
def test_submit_drive_empty_then_run_falls_through_to_vision(conn, indexed, monkeypatch):
    root, document_id = indexed
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "tesseract v5.4.0")
    monkeypatch.setattr(ocr, "_render_page", lambda page, tmp_dir: Path(tmp_dir) / "page-1.png")
    with_drive = ocr.OcrConfig(ladder=("local", "drive", "vision"), min_confidence=0.75)
    ocr.run_ocr(
        conn,
        root,
        config=with_drive,
        extractor=lambda page: "",
        runner=lambda image_path: ("weak", 0.2),
        now=NOW,
    )
    ocr.submit_drive_text(conn, document_id, "", now=NOW)
    assert _pages(conn, document_id)[0]["note"] == "drive_empty"

    summary = ocr.run_ocr(conn, root, config=with_drive, extractor=_never_called, now=NOW)
    assert summary.pending_vision == 1  # resumes strictly after `drive`, never retrying it
    page = _pages(conn, document_id)[0]
    assert (page["status"], page["rung"], page["text"]) == ("pending_vision", "vision", "weak")
