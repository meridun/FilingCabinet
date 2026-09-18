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


def _cli(db, *args, config=None, stdin=None, check=True):
    """The real CLI in a real subprocess; `config` and `stdin` are for the drive rung."""
    cmd = [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "--json"]
    if config is not None:
        cmd += ["--config", str(config)]
    return subprocess.run(
        [*cmd, *args], capture_output=True, text=True, input=stdin, check=check
    )


def _run(db, *args, **kwargs):
    return json.loads(_cli(db, *args, **kwargs).stdout)


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


def _write_pdf(path, pages, render_mode=0):
    """One page per element; an element may be a single line or a list of lines.

    ``render_mode=3`` writes the text invisibly - the shape a thin scanner-embedded OCR layer
    has: the text layer carries it, but a rasterized page shows tesseract nothing.
    """
    import fitz

    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 100
        for line in [lines] if isinstance(lines, str) else lines:
            page.insert_text((72, y), line, fontsize=9, render_mode=render_mode)
            y += 14
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


def _query(db, sql, params):
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _pages_of(db, document_id):
    return {
        row["page_number"]: row
        for row in _query(
            db,
            "SELECT page_number, status, rung, ocr_source, confidence, text, note "
            "FROM page_ocr WHERE document_id = ? ORDER BY page_number",
            (document_id,),
        )
    }


# Drive returns image files with this machine annotation appended; it must not reach page_ocr.
DRIVE_BLOB = (
    "Gas meter reading 41215 recorded at the Camden depot on 3 February 2026.\n"
    "Image labels: this interior mention belongs to the document and must survive.\n"
    "The engineer signature block closes the second page.\n"
    "\nImage labels: [receipt, text, document]\n"
)


@requires_pymupdf
def test_drive_rung_smoke_end_to_end(tmp_path):
    """Issue #9's acceptance criteria, walked through the real CLI over a real tree.

    The gating real run for the agent-driven `drive` rung: a ladder that parks pages at
    `pending_drive` instead of `pending_vision`, a document-level submit resolved by sha256,
    the `Image labels:` trailer stripped, an empty Drive answer leaving the local read as the
    fallback, and the document tree untouched throughout (``docs/Architecture.md`` section 6).
    """
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    (root / "scans").mkdir(parents=True)
    (root / "archive").mkdir(parents=True)
    # Three pages: a dense text layer, then two the local rung cannot read.
    _write_pdf(
        root / "scans" / "invoice.pdf",
        [[LETTER[i : i + 90] for i in range(0, len(LETTER), 90)], "", ""],
    )
    # Same basename in another directory: the ambiguous-title case the spike found on Drive.
    # Its text layer is thin and invisible, so the local rung carries a low-confidence read it
    # cannot clear the bar with, whether or not this host has tesseract.
    _write_pdf(root / "archive" / "invoice.pdf", ["Faint carbon copy receipt zzqq"], render_mode=3)

    config = tmp_path / "config.toml"
    config.write_text(
        '[paths]\nroot = "%s"\n\n[ocr]\nladder = ["local", "drive", "vision"]\n'
        % root.as_posix(),
        encoding="utf-8",
    )

    assert _run(db, "migrate", "--create")["created"] is True
    assert _run(db, "ingest", "--root", str(root))["new"] == 2
    fingerprint = _tree_fingerprint(root)

    invoice = _document_id_of(db, "scans/invoice.pdf")
    faint = _document_id_of(db, "archive/invoice.pdf")
    sha = _query(db, "SELECT sha256 FROM document WHERE document_id = ?", (invoice,))[0]["sha256"]

    # AC 3: with `drive` before `vision`, deferred pages park at `pending_drive`.
    first = _run(db, "ocr", "run", "--root", str(root), config=config)
    assert first["errors"] == 0 and first["pending_vision"] == 0
    assert first["pending_drive"] == 3  # two blank invoice pages + the faint receipt
    assert first["pages"] == first["ok"] + first["pending_drive"] + first["skipped"] + \
        first["exhausted"]
    pages = _pages_of(db, invoice)
    assert pages[1]["status"] == "ok"  # the text layer finished at the local rung
    assert [pages[2]["status"], pages[3]["status"]] == ["pending_drive", "pending_drive"]
    # The thin local read is carried onto the deferred row rather than blanked.
    carried = _pages_of(db, faint)[1]
    assert carried["status"] == "pending_drive" and (carried["text"] or "").strip()

    # AC 3: a page waiting for the agent is sticky - a re-run must not escalate it to vision.
    _run(db, "ocr", "run", "--root", str(root), config=config)
    assert _pages_of(db, invoice)[2]["status"] == "pending_drive"
    assert _pages_of(db, faint)[1]["status"] == "pending_drive"

    # AC 2: a bare filename is a title, and a title is ambiguous - never silently matched.
    refused = _cli(
        db, "ocr", "submit", "--source", "drive", "--rel-path", "invoice.pdf",
        "--text-file", "-", config=config, stdin=DRIVE_BLOB, check=False,
    )
    assert refused.returncode != 0 and "ambiguous" in refused.stderr

    # AC 1, 4, 6: one document-level submit, resolved by sha256, marks every pending page.
    submitted = _run(
        db, "ocr", "submit", "--source", "drive", "--sha256", sha,
        "--text-file", "-", config=config, stdin=DRIVE_BLOB,
    )
    assert (submitted["pages_marked"], submitted["text_page"]) == (2, 2)
    assert submitted["ocr_source"] == "drive"
    pages = _pages_of(db, invoice)
    assert [pages[n]["status"] for n in (1, 2, 3)] == ["ok", "ok", "ok"]
    assert [pages[n]["ocr_source"] for n in (2, 3)] == ["drive", "drive"]
    assert "Image labels: [receipt" not in pages[2]["text"]  # AC 4: the trailer is gone
    assert "Image labels: this interior mention" in pages[2]["text"]  # ... only the trailer
    assert "Northwind" in pages[1]["text"]  # the local read is untouched
    document = _query(db, "SELECT ocr_source FROM document WHERE document_id = ?", (invoice,))
    assert document[0]["ocr_source"] == "drive"
    assert _run(db, "find", "41215")["count"] == 1  # the drive text is indexed
    assert _run(db, "find", "northwind")["count"] == 1  # ... and the local text still is

    # A re-submit is a no-op, not an error: nothing is left pending.
    assert _run(
        db, "ocr", "submit", "--source", "drive", "--sha256", sha,
        "--text-file", "-", config=config, stdin=DRIVE_BLOB,
    )["pages_marked"] == 0

    # AC 5: Drive OCR is opportunistic; an empty answer must not blank the local fallback.
    before = _pages_of(db, faint)[1]
    empty = _run(
        db, "ocr", "submit", "--source", "drive", "--rel-path", "archive/invoice.pdf",
        "--text-file", "-", config=config, stdin="\n  \nImage labels: [photo]\n",
    )
    assert empty["empty"] is True and empty["pages_marked"] == 1
    after = _pages_of(db, faint)[1]
    assert (after["text"], after["confidence"], after["ocr_source"]) == (
        before["text"], before["confidence"], before["ocr_source"]
    )
    assert (after["status"], after["note"]) == ("skipped", "drive_empty")
    # ... and the page is no longer sticky, so the next run resumes strictly after `drive`.
    _run(db, "ocr", "run", "--root", str(root), config=config)
    assert _pages_of(db, faint)[1]["status"] == "pending_vision"

    # AC 7 / Architecture section 6: the whole exchange wrote only the index.
    assert _tree_fingerprint(root) == fingerprint
