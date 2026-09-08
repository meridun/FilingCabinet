import hashlib
import shutil
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
        config=ocr.OcrConfig(ladder=("drive", "local"), min_confidence=0.75),
        extractor=lambda page: CLEAN_TEXT,
        runner=_never_called,
        now=NOW,
    )
    assert summary.ok == 1  # the reserved `drive` rung was stepped over, local still ran
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
