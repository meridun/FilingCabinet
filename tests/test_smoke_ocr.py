"""End-to-end smoke: the real `ocr`, `find`, and `doctor` verbs over a real tree.

The repeatable gating real-run for phase-4 OCR, the sibling of ``tests/test_smoke_ingest.py``
and ``tests/test_smoke_dupes.py``: it invokes ``python -m filingcabinet.cli`` exactly as a
scheduled run would rather than calling ``cli.main`` in-process, and walks issue #4's
acceptance criteria in order -- migrate, ingest, `ocr run` over a text-layer document, the
resumable re-run, the vision rung via `ocr submit`, `find --json`, and `doctor` -- asserting
throughout that the document tree itself is untouched (``docs/Architecture.md`` section 6).

Authoring the fixtures needs PyMuPDF (the optional ``ocr`` extra); that part is skipped where
it is absent. `doctor` and the migration always run: reporting a missing toolchain without
failing is itself an acceptance criterion.
"""

import hashlib
import json
import subprocess
import sys

import pytest

from filingcabinet import ocr

requires_pymupdf = pytest.mark.skipif(
    ocr.pymupdf_version() is None, reason="optional `ocr` extra (PyMuPDF)"
)

# Dense, fully legible prose: the embedded text layer clears the default 0.75 bar, so the
# local rung finishes without ever shelling out to tesseract.
LETTER = (
    "Northwind Supplies invoice 2026-014 for office consumables delivered to the Camden "
    "depot on 3 February 2026. Payment is due within thirty days by bank transfer to the "
    "account named on this page. Please quote the invoice number on every remittance."
)


def _run(db, *args):
    proc = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "--json", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _tree_fingerprint(root):
    return {
        p.relative_to(root).as_posix(): (
            p.stat().st_size,
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _write_pdf(path, lines):
    import fitz

    doc = fitz.open()
    for line in lines:
        page = doc.new_page()
        page.insert_text((72, 100), line, fontsize=9)
    doc.save(path)
    doc.close()


def test_doctor_smoke_reports_the_toolchain(tmp_path):
    db = tmp_path / "fc.db"
    assert _run(db, "migrate", "--create")["created"] is True
    report = _run(db, "doctor")
    assert set(report["tesseract"]) == {"present", "version"}
    assert isinstance(report["tesseract"]["present"], bool)
    assert report["ladder"] == ["local", "vision"] and report["migrated"] is True


@requires_pymupdf
def test_ocr_smoke_end_to_end(tmp_path):
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    root.mkdir()
    # Wrapped at ~90 characters: several lines of a real text layer on one page.
    _write_pdf(root / "invoice.pdf", [LETTER[i : i + 90] for i in range(0, len(LETTER), 90)])
    _write_pdf(root / "blank.pdf", [""])  # no text layer: escalates past the local rung

    assert _run(db, "migrate", "--create")["created"] is True
    ingested = _run(db, "ingest", "--root", str(root))
    assert (ingested["scanned"], ingested["new"]) == (2, 2)

    fingerprint = _tree_fingerprint(root)

    first = _run(db, "ocr", "run", "--root", str(root))
    assert first["documents"] == 2 and first["errors"] == 0
    assert first["ok"] >= 1  # the text-layer page never needed tesseract
    assert first["pages"] == first["ok"] + first["pending_vision"] + first["skipped"] + \
        first["exhausted"]

    found = _run(db, "find", "northwind")
    assert found["count"] == 1
    hit = found["hits"][0]
    assert hit["rel_path"] == "invoice.pdf" and "[Northwind]" in hit["snippet"]

    second = _run(db, "ocr", "run", "--root", str(root))
    # Resumable: a settled page is never re-read. A page parked because the toolchain was
    # missing stays retryable, so on a tesseract-less host the blank page is attempted again.
    assert second["ok"] == 0 and second["pages"] <= 1

    # The blank page has no text to search for, so its id comes from the index directly -
    # every action below is still the real CLI in a real subprocess.
    blank_id = _document_id_of(db, "blank.pdf")
    text_file = tmp_path / "vision.txt"
    text_file.write_text("meter reading 41215 taken by hand", encoding="utf-8")
    submitted = _run(
        db, "ocr", "submit", "--document", str(blank_id), "--page", "1",
        "--text-file", str(text_file),
    )
    assert submitted["ocr_source"] == "vision"

    vision_hits = _run(db, "find", "meter reading")
    assert vision_hits["count"] == 1
    assert vision_hits["hits"][0]["rel_path"] == "blank.pdf"
    assert _run(db, "find", "northwind")["count"] == 1  # the invoice is still indexed

    assert _tree_fingerprint(root) == fingerprint  # OCR read the tree and wrote only the index


def _document_id_of(db, rel_path):
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT document_id FROM occurrence WHERE rel_path = ?", (rel_path,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])
