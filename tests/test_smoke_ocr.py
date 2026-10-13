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

    The gating real run for the agent-submit path of the `drive` rung: a ladder with `drive`
    but no Drive authorization (the rung reports itself unavailable and the pages stay
    retryable), a document-level submit resolved by sha256,
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

    # With `drive` before `vision` but no Drive authorization (no data_dir here), the rung is
    # unavailable: deferred pages move on to `pending_vision` carrying the reason, count as
    # degraded, and stay open to the agent's `--source drive` submit below.
    first = _run(db, "ocr", "run", "--root", str(root), config=config)
    assert first["errors"] == 0 and first["pending_drive"] == 0
    assert first["pending_vision"] == 3  # two blank invoice pages + the faint receipt
    assert first["degraded"] == 3 and first["drive"]["available"] is False
    assert first["pages"] == first["ok"] + first["pending_vision"] + first["skipped"] + \
        first["exhausted"]
    pages = _pages_of(db, invoice)
    assert pages[1]["status"] == "ok"  # the text layer finished at the local rung
    assert [pages[n]["note"] for n in (2, 3)] == ["rung_unavailable:drive"] * 2
    # The thin local read is carried onto the deferred row rather than blanked.
    carried = _pages_of(db, faint)[1]
    assert carried["status"] == "pending_vision" and (carried["text"] or "").strip()
    assert carried["note"] == "rung_unavailable:drive"

    # A re-run retries the unavailable rung rather than writing the pages off, and keeps the read.
    again = _run(db, "ocr", "run", "--root", str(root), config=config)
    assert (again["pages"], again["degraded"]) == (3, 3)
    assert _pages_of(db, faint)[1]["text"] == carried["text"]

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


# Synthetic stand-ins for credential values, written into the test's own data_dir. They exist
# only so the smoke can assert that no report ever echoes a credential back.
PLANTED_SECRET = "planted-client-secret-7f3c"
PLANTED_TOKEN = "planted-refresh-token-9a1e"


@requires_pymupdf
def test_drive_api_rung_smoke_end_to_end(tmp_path, fake_drive):
    """Issue #32's acceptance criteria over a real tree, a real index and the real CLI.

    The gating real run for the unattended `drive` rung. `doctor` and `ocr auth` run as real
    subprocesses with credential files in a local-only `data_dir`; the ladder walk runs the real
    engine over the CLI-built index with the in-memory Drive (no network, no real credential):
    the same title in two folders resolved by path and size, the document-level blob with the
    trailer stripped, Drive writes confined to the marked temp folder and deleted, and the
    document tree untouched throughout (``docs/Architecture.md`` section 6).
    """
    from filingcabinet import db as db_mod
    from filingcabinet import drive, ocr as ocr_mod

    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    data_dir = tmp_path / "data"
    snapshot_dir = tmp_path / "snap"
    for directory in (root / "scans", root / "archive", data_dir, snapshot_dir):
        directory.mkdir(parents=True)
    # A dense text-layer page, then two the local rung cannot read.
    _write_pdf(
        root / "scans" / "statement.pdf",
        [[LETTER[i : i + 90] for i in range(0, len(LETTER), 90)], "", ""],
    )
    # Same title in another folder, different bytes: identity must come from path and size.
    _write_pdf(root / "archive" / "statement.pdf", ["Faint carbon copy receipt zzqq"],
               render_mode=3)

    def write_config(path, data):
        path.write_text(
            "[paths]\nroot = \"%s\"\ndata_dir = \"%s\"\nsnapshot_dir = \"%s\"\n\n"
            "[ocr]\nladder = [\"local\", \"drive\", \"vision\"]\n\n"
            "[ingest]\nexclude = [\".*\", \"~$*\", \"*.tmp\", \"FilingCabinet-OCR-Tmp\"]\n"
            % (root.as_posix(), data.as_posix(), snapshot_dir.as_posix()),
            encoding="utf-8",
        )
        return path

    config = write_config(tmp_path / "config.toml", data_dir)
    assert _run(db, "migrate", "--create")["created"] is True
    assert _run(db, "ingest", "--root", str(root), config=config)["new"] == 2
    fingerprint = _tree_fingerprint(root)

    # `doctor`: presence and scope names only, in both outputs; the case-insensitive exclude
    # covers the temp folder, so there is nothing to warn about. Exit code stays 0.
    (data_dir / drive.CLIENT_SECRET_FILENAME).write_text(
        '{"installed": {"client_id": "x", "client_secret": "%s"}}' % PLANTED_SECRET,
        encoding="utf-8",
    )
    (data_dir / drive.TOKEN_FILENAME).write_text(
        '{"refresh_token": "%s", "client_secret": "%s", "scopes": ["%s", "%s"]}'
        % (PLANTED_TOKEN, PLANTED_SECRET, *drive.SCOPES),
        encoding="utf-8",
    )
    report = _cli(db, "doctor", config=config)
    plain = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "--config", str(config),
         "doctor"],
        capture_output=True, text=True, check=True,
    )
    for output in (report.stdout, report.stderr, plain.stdout, plain.stderr):
        assert PLANTED_SECRET not in output and PLANTED_TOKEN not in output
    block = json.loads(report.stdout)["drive"]
    assert (block["client_secret"], block["token"]) == (True, True)
    assert block["scopes"] == ["drive.file", "drive.readonly"]
    assert (block["temp_folder_excluded"], block["data_dir_synced"], block["warnings"]) == (
        True, False, []
    )
    assert "client secret present, token present" in plain.stdout

    # `ocr auth` refuses a data_dir Drive would sync, before writing anything anywhere.
    synced = write_config(tmp_path / "synced.toml", root / "creds")
    refused = _cli(db, "ocr", "auth", "--client-secret",
                   str(data_dir / drive.CLIENT_SECRET_FILENAME), config=synced, check=False)
    assert refused.returncode != 0 and "Drive syncs" in refused.stderr
    assert PLANTED_SECRET not in refused.stderr + refused.stdout
    assert _tree_fingerprint(root) == fingerprint and not (root / "creds").exists()

    # The unattended walk: real PyMuPDF and index, in-memory Drive.
    blob = "Statement balance 9921 carried forward.\n\nImage labels: [document, text]\n"
    fake_drive.add_path("scans/statement.pdf",
                        size=(root / "scans" / "statement.pdf").stat().st_size, text=blob)
    fake_drive.add_path("archive/statement.pdf",
                        size=(root / "archive" / "statement.pdf").stat().st_size,
                        text="Carbon copy 5531")
    session = drive.DriveSession(drive.DriveConfig(), fake_drive)
    conn = db_mod.connect(db)
    try:
        summary = ocr_mod.run_ocr(
            conn, root, config=ocr_mod.OcrConfig(ladder=("local", "drive", "vision")),
            drive=session,
        )
    finally:
        conn.close()
    assert (summary.errors, summary.pending_vision, summary.pending_drive, summary.degraded) == (
        0, 0, 0, 0
    )
    assert (session.fetched, session.failed, session.delete_failures) == (2, 0, 0)

    scans = _pages_of(db, _document_id_of(db, "scans/statement.pdf"))
    assert scans[1]["rung"] == "local" and "Northwind" in scans[1]["text"]
    assert (scans[2]["status"], scans[2]["rung"], scans[2]["ocr_source"]) == (
        "ok", "drive", "drive"
    )
    assert scans[2]["text"].startswith("Statement balance 9921")
    assert "Image labels" not in scans[2]["text"]  # the trailer is stripped
    assert scans[2]["confidence"] == ocr_mod.DRIVE_CONFIDENCE
    assert (scans[3]["status"], scans[3]["rung"]) == ("ok", "drive")  # resolved, one fetch
    archive = _pages_of(db, _document_id_of(db, "archive/statement.pdf"))
    assert archive[1]["text"] == "Carbon copy 5531"  # its own blob, not the other title's

    # Drive writes: one marked temp folder, a copy per document inside it, each deleted.
    writes = fake_drive.writes()
    folders = [call for call in writes if call[0] == "create_marked_folder"]
    assert folders == [("create_marked_folder", drive.DEFAULT_TEMP_FOLDER)]
    temp_id = drive.DriveSession(drive.DriveConfig(), fake_drive)._temp_folder()
    copies = [call for call in writes if call[0] == "copy_as_doc"]
    assert len(copies) == 2 and {call[2] for call in copies} == {temp_id}
    assert not [n for n in fake_drive.nodes.values() if n["mark"] == drive.COPY_MARK]

    assert _run(db, "find", "9921")["count"] == 1  # the drive text is indexed
    assert _run(db, "find", "northwind")["count"] == 1  # ... and the local read still is

    # A CLI re-run has nothing left to walk; without a token it reports why, and still exits 0.
    (data_dir / drive.TOKEN_FILENAME).unlink()
    again = _run(db, "ocr", "run", "--root", str(root), config=config)
    assert again["pages"] == 0 and again["drive"]["available"] is False

    assert _tree_fingerprint(root) == fingerprint  # the whole exchange wrote only the index
