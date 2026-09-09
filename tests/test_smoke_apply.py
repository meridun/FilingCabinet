"""End-to-end smoke: the real `apply` and `undo` verbs over a real tree.

The repeatable gating real-run for phase 6, the sibling of ``tests/test_smoke_propose.py``: it
invokes ``python -m filingcabinet.cli`` by subprocess exactly as a scheduled run would rather
than calling ``cli.main`` in-process, and walks issue #6's acceptance criteria in order --
migrate, ingest, `ocr run`, `propose`, `apply --dry-run` (tree byte-identical), `apply` (files
moved, ``move_log`` populated, the index consistent), then `undo` (the tree byte-identical to
the pre-apply fingerprint again, ``undone_at`` set, a second `undo` a no-op).

This is the phase where the tool finally moves a document, so the invariant it proves is the
positive half of ``docs/Architecture.md`` section 6: every move traces to an explicit plan file
argument, and every move is reversible. Authoring the PDF fixtures needs PyMuPDF (the optional
``ocr`` extra); that part is skipped where it is absent. The no-implicit-apply guard always runs.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from filingcabinet import ocr

requires_pymupdf = pytest.mark.skipif(
    ocr.pymupdf_version() is None, reason="optional `ocr` extra (PyMuPDF)"
)

INVOICE = (
    "Northwind Supplies tax invoice no 2026-014 for office consumables delivered to the "
    "Camden depot on 3 February 2026. Payment is due within thirty days by bank transfer "
    "to the account named on this page. Please quote the invoice number on every remittance."
)
LETTER = (
    "Dear neighbour, the hedge along the boundary will be trimmed next Tuesday and the "
    "clippings taken away the same afternoon. No action is needed from you. Kind regards "
    "from the household at number eleven, written by hand on plain unheaded paper."
)

TAXONOMY = """
version = 1
doc_types = ["invoice", "statement"]
date_order = "dmy"

[parties.northwind]
display = "Northwind Supplies"
aliases = ["northwind supplies"]

[[rules]]
id = "northwind-invoice"
party = "northwind"
doc_type = "invoice"
all = ["northwind"]
any = ["invoice", "tax invoice"]
folder = "Suppliers/Northwind"
tags = ["supplier"]
priority = 100
"""


def _run(db, *args):
    proc = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "--json", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _content_fingerprint(root):
    """Path -> content hash. Deliberately not mtime: a move preserves bytes, not stat()."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _rows(db, sql):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def _write_pdf(path, lines):
    import fitz

    doc = fitz.open()
    for line in lines:
        page = doc.new_page()
        page.insert_text((72, 100), line, fontsize=9)
    doc.save(path)
    doc.close()


def _wrap(text):
    return [text[i : i + 90] for i in range(0, len(text), 90)]


def test_apply_smoke_never_acts_without_an_explicit_plan(tmp_path):
    """The AC-5 guard runs with no toolchain at all: there is no implicit 'apply everything'."""
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    root.mkdir()
    assert _run(db, "migrate", "--create")["created"] is True
    for argv in (["apply"], ["undo"]):
        proc = subprocess.run(
            [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), *argv,
             "--root", str(root)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2 and "arguments are required" in proc.stderr


@requires_pymupdf
def test_apply_and_undo_smoke_end_to_end(tmp_path):
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    root.mkdir()
    (root / "inbox").mkdir()
    _write_pdf(root / "inbox" / "scan-001.pdf", _wrap(INVOICE))
    _write_pdf(root / "inbox" / "scan-002.pdf", _wrap(LETTER))
    taxonomy = tmp_path / "taxonomy.toml"
    taxonomy.write_text(TAXONOMY, encoding="utf-8")
    plan_dir = tmp_path / "plans"  # deliberately outside root

    assert _run(db, "migrate", "--create")["created"] is True
    assert _run(db, "ingest", "--root", str(root))["new"] == 2
    assert _run(db, "ocr", "run", "--root", str(root))["errors"] == 0

    proposed = _run(db, "propose", "--root", str(root), "--taxonomy", str(taxonomy),
                    "--plan-dir", str(plan_dir))
    plan_path = proposed["plan"]
    plan_id = proposed["plan_id"]
    assert (proposed["move"], proposed["unclassified"]) == (1, 1)
    target = "Suppliers/Northwind/2026-02-03_Northwind_Supplies_invoice.pdf"

    before = _content_fingerprint(root)
    assert set(before) == {"inbox/scan-001.pdf", "inbox/scan-002.pdf"}

    # --dry-run reports the move and leaves the tree and the log untouched.
    dry = _run(db, "apply", plan_path, "--root", str(root), "--dry-run")
    assert dry["dry_run"] is True and dry["moved"] == 1 and dry["ignored"] == 1
    assert _content_fingerprint(root) == before
    assert _rows(db, "SELECT * FROM move_log") == []

    applied = _run(db, "apply", plan_path, "--root", str(root))
    assert (applied["moved"], applied["ignored"], applied["skipped"]) == (1, 1, 0)
    assert applied["errors"] == 0 and applied["plan_id"] == plan_id
    after = _content_fingerprint(root)
    assert set(after) == {target, "inbox/scan-002.pdf"}
    assert after[target] == before["inbox/scan-001.pdf"]  # the bytes are the same document

    log = _rows(db, "SELECT * FROM move_log")
    assert len(log) == 1 and log[0]["plan_id"] == plan_id and log[0]["undone_at"] is None
    assert (log[0]["from_path"], log[0]["to_path"]) == ("inbox/scan-001.pdf", target)

    # The index followed the file, and `apply` is where the plan's fields land on `document`.
    paths = {row["rel_path"] for row in _rows(db, "SELECT rel_path FROM occurrence")}
    assert paths == {target, "inbox/scan-002.pdf"}
    moved_doc = _rows(db, f"SELECT * FROM document WHERE document_id = {log[0]['document_id']}")[0]
    assert (moved_doc["doc_date"], moved_doc["party"]) == ("2026-02-03", "Northwind Supplies")
    assert moved_doc["doc_type"] == "invoice"

    # A re-`apply` of the same plan is not a second move: the file is no longer where the plan
    # says it is, so the entry is skipped and reported rather than retried.
    repeat = _run(db, "apply", plan_path, "--root", str(root))
    assert repeat["moved"] == 0 and repeat["skipped"] == 1
    assert _content_fingerprint(root) == after
    assert len(_rows(db, "SELECT * FROM move_log")) == 1

    undone_dry = _run(db, "undo", plan_id, "--root", str(root), "--dry-run")
    assert undone_dry["reversed"] == 1 and _content_fingerprint(root) == after

    undone = _run(db, "undo", plan_id, "--root", str(root))
    assert undone["reversed"] == 1 and undone["errors"] == 0
    assert _content_fingerprint(root) == before  # byte-identical to the pre-apply tree
    log = _rows(db, "SELECT * FROM move_log")
    assert len(log) == 1 and log[0]["undone_at"] is not None
    paths = {row["rel_path"] for row in _rows(db, "SELECT rel_path FROM occurrence")}
    assert paths == {"inbox/scan-001.pdf", "inbox/scan-002.pdf"}

    again = _run(db, "undo", plan_id, "--root", str(root))
    assert again["reversed"] == 0 and again["entries"] == []
    assert _content_fingerprint(root) == before
